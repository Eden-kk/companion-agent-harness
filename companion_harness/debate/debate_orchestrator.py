"""DebateOrchestrator — lockstep two-session debate loop.

KV rollback on barge-in is wired through `MiniCPMDuplexSession.restore_snapshot()`,
which delegates to `base.restore_speculative_snapshot()`. `generate()` now assigns
the return of `save_speculative_snapshot()` to `base._speculative_snapshot` before
each duplex tick (the earlier no-op was discarding the return value).
Requires dual load-mode (separate base model per session) for per-session isolation.
See artifacts/snapshot_api_verdict_v2.json for probe details.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np

from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES, DuplexTickResult

SILENCE = np.zeros(CHUNK_SAMPLES, np.float32)


@dataclass
class TickRecord:
    tick: int
    audible: list[str]
    floor: str | None
    break_fired_this_tick: list[str]
    break_armed_for_next_tick: list[str]
    silence_run: int
    moderator_nudge_fired: bool
    per_speaker: dict[str, dict]
    rolled_back_this_tick: list[str] = field(default_factory=list)


@dataclass
class DebateMetrics:
    total_ticks: int = 0
    collision_count: int = 0
    max_collision_duration_ticks: int = 0
    harness_forced_breaks: int = 0
    moderator_nudges_fired: int = 0
    deadlocks_detected: int = 0
    self_yields: int = 0
    barge_in_attempts: int = 0
    barge_in_successes: int = 0
    turn_count_per_speaker: dict[str, int] = field(default_factory=dict)
    t_max_hits: int = 0
    silence_terminated: bool = False
    rollbacks_performed: int = 0
    barge_text_signals_sent: int = 0


@dataclass
class DebateTrace:
    motion: str
    ticks: list[TickRecord]
    metrics: DebateMetrics
    k_grace: int
    t_max: int
    n_deadlock: int


def _compute_metrics(ticks: list[TickRecord], *, silence_terminated: bool = False) -> DebateMetrics:
    total_ticks = len(ticks)
    collision_count = sum(1 for t in ticks if len(t.audible) >= 2)
    harness_forced_breaks = sum(len(t.break_fired_this_tick) for t in ticks)
    moderator_nudges_fired = sum(1 for t in ticks if t.moderator_nudge_fired)
    deadlocks_detected = moderator_nudges_fired

    # longest contiguous collision run
    max_run = 0
    run = 0
    for t in ticks:
        if len(t.audible) >= 2:
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0

    # barge_in_attempts: collision just started (overlap edge: prev was single-speaker)
    barge_in_attempts = 0
    for i in range(1, len(ticks)):
        if len(ticks[i].audible) >= 2 and len(ticks[i - 1].audible) == 1:
            barge_in_attempts += 1

    # barge_in_successes: collision resolved, floor's challenger won (break fired)
    barge_in_successes = 0
    for i in range(1, len(ticks)):
        prev, cur = ticks[i - 1], ticks[i]
        if (
            len(prev.audible) >= 2
            and len(cur.audible) == 1
            and cur.break_fired_this_tick
            and cur.audible[0] == prev.floor
        ):
            barge_in_successes += 1

    # self_yields: overlap resolved without a break — incumbent ceded voluntarily
    self_yields = 0
    for i in range(1, len(ticks)):
        prev, cur = ticks[i - 1], ticks[i]
        if (
            len(prev.audible) >= 2
            and len(cur.audible) == 1
            and not cur.break_fired_this_tick
        ):
            self_yields += 1

    # turn_count_per_speaker: count floor transitions to a named speaker
    all_names: set[str] = set()
    for t in ticks:
        all_names.update(t.per_speaker.keys())
    turn_count: dict[str, int] = {n: 0 for n in sorted(all_names)}
    prev_floor: str | None = None
    for t in ticks:
        if t.floor is not None and t.floor != prev_floor:
            turn_count[t.floor] = turn_count.get(t.floor, 0) + 1
        prev_floor = t.floor

    # t_max_hits: break was armed with no collision on that tick (T_MAX backstop fired)
    t_max_hits = sum(
        1
        for t in ticks
        if t.break_armed_for_next_tick and len(t.audible) < 2
    )

    rollbacks_performed = sum(len(t.rolled_back_this_tick) for t in ticks)
    # barge text signal is queued on every collision-resolution (independent of
    # whether the KV rollback actually fired — see module docstring for the
    # MiniCPMO-side limitation).
    barge_text_signals_sent = sum(
        1
        for i in range(1, len(ticks))
        if len(ticks[i - 1].audible) >= 2 and ticks[i].break_fired_this_tick
    )

    return DebateMetrics(
        total_ticks=total_ticks,
        collision_count=collision_count,
        max_collision_duration_ticks=max_run,
        harness_forced_breaks=harness_forced_breaks,
        moderator_nudges_fired=moderator_nudges_fired,
        deadlocks_detected=deadlocks_detected,
        self_yields=self_yields,
        barge_in_attempts=barge_in_attempts,
        barge_in_successes=barge_in_successes,
        turn_count_per_speaker=turn_count,
        t_max_hits=t_max_hits,
        silence_terminated=silence_terminated,
        rollbacks_performed=rollbacks_performed,
        barge_text_signals_sent=barge_text_signals_sent,
    )


class DebateOrchestrator:
    def __init__(
        self,
        motion: str,
        sessions: dict[str, Any],
        *,
        listen_prob_scale: dict[str, float] | None = None,
        k_grace: int = 0,
        n_deadlock: int = 8,
        silence_terminate_ticks: int = 5,
        t_max: int = 0,
        max_ticks: int = 60,
        moderator_seed_audio: np.ndarray | None = None,
        moderator_nudge_audio: np.ndarray | None = None,
        rng_seed: int = 0,
        plan_b_text_supplier: Callable[[dict[str, DuplexTickResult], str], str | None] | None = None,
    ) -> None:
        self._motion = motion
        self._sessions = dict(sessions)
        self._listen_prob_scale = listen_prob_scale or {n: 1.0 for n in sessions}
        self._k_grace = k_grace
        self._n_deadlock = n_deadlock
        self._silence_terminate_ticks = silence_terminate_ticks
        self._t_max = t_max
        self._max_ticks = max_ticks
        self._moderator_seed_audio = (
            moderator_seed_audio if moderator_seed_audio is not None else np.zeros(CHUNK_SAMPLES, np.float32)
        )
        self._moderator_nudge_audio = (
            moderator_nudge_audio if moderator_nudge_audio is not None else np.zeros(CHUNK_SAMPLES, np.float32)
        )
        self._rng = random.Random(rng_seed)
        self._plan_b_text_supplier = plan_b_text_supplier

    def run(self) -> DebateTrace:
        self._inbox: dict[str, np.ndarray] = {n: self._moderator_seed_audio.copy() for n in self._sessions}
        self._floor: str | None = None
        self._turn_len: dict[str, int] = {n: 0 for n in self._sessions}
        self._overlap: dict[str, int] = {n: 0 for n in self._sessions}
        self._silence_run: int = 0
        self._break_armed: set[str] = set()
        self._ticks: list[TickRecord] = []
        self._last_results: dict[str, DuplexTickResult] = {}
        self._pending_barge_text: dict[str, str | None] = {n: None for n in self._sessions}
        self._terminate_now: bool = False

        for t in range(self._max_ticks):
            self._tick(t)
            if self._terminate_now:
                break

        metrics = _compute_metrics(self._ticks, silence_terminated=self._terminate_now)
        return DebateTrace(
            motion=self._motion,
            ticks=self._ticks,
            metrics=metrics,
            k_grace=self._k_grace,
            t_max=self._t_max,
            n_deadlock=self._n_deadlock,
        )

    def _tick(self, t: int) -> None:
        # PHASE 1: PERCEIVE
        # A break-armed session no-ops its prefill internally (modeling_minicpmo.py
        # :2816), so DO NOT consume pending_barge_text on the break-fire tick or
        # both the audio AND the text_list would be silently discarded. Carry it
        # over to the next tick when the break has cleared and prefill will
        # actually ingest the input.
        for name, sess in self._sessions.items():
            barge_text = self._pending_barge_text.get(name)
            is_break_firing = name in self._break_armed
            if barge_text is not None and not is_break_firing:
                self._pending_barge_text[name] = None
                sess.prefill(self._inbox[name], text_list=[barge_text])
            elif self._plan_b_text_supplier is not None:
                text_seed = self._plan_b_text_supplier(self._last_results, name)
                sess.prefill(self._inbox[name], text_list=[text_seed] if text_seed else None)
            else:
                sess.prefill(self._inbox[name])

        # PHASE 2: GENERATE
        results: dict[str, DuplexTickResult] = {}
        for name, sess in self._sessions.items():
            results[name] = sess.generate(listen_prob_scale=self._listen_prob_scale[name])

        fired = set(self._break_armed)
        for name in fired:
            self._sessions[name].clear_break()
        self._break_armed.clear()

        spoke = {n: not r.is_listen for n, r in results.items()}
        audible = sorted(n for n in spoke if spoke[n])

        # PHASE 3: ARBITRATE
        rolled_back: list[str] = []

        if not audible:
            self._silence_run += 1
            if self._silence_run >= self._silence_terminate_ticks:
                self._terminate_now = True
        elif len(audible) == 1:
            s = audible[0]
            self._silence_run = 0
            if self._floor != s:
                self._turn_len = {n: 0 for n in self._sessions}
            self._turn_len[s] += 1
            self._floor = None if results[s].end_of_turn else s
            self._overlap = {n: 0 for n in self._sessions}
        else:
            self._silence_run = 0
            if self._floor is None:
                self._floor = self._tie_breaker(t)
            challenger = next(n for n in self._sessions if n != self._floor)
            self._overlap[challenger] += 1
            self._turn_len[self._floor] += 1
            if self._overlap[challenger] > self._k_grace:
                self._arm_break(self._floor)
                rolled = self._sessions[self._floor].restore_snapshot()
                if rolled:
                    rolled_back.append(self._floor)
                self._pending_barge_text[self._floor] = results[challenger].text
                self._floor = challenger

        if self._t_max and self._floor and spoke.get(self._floor) and self._turn_len[self._floor] >= self._t_max:
            self._arm_break(self._floor)
            self._turn_len[self._floor] = 0

        # PHASE 4: PLUMB
        emitted = {
            n: (self._one_sec(results[n].audio_waveform) if spoke[n] else SILENCE.copy())
            for n in results
        }
        self._inbox = {
            n: sum((emitted[o] for o in results if o != n), SILENCE.copy())
            for n in results
        }
        self._last_results = results

        self._ticks.append(TickRecord(
            tick=t,
            audible=audible,
            floor=self._floor,
            break_fired_this_tick=sorted(fired),
            break_armed_for_next_tick=sorted(self._break_armed),
            silence_run=self._silence_run,
            moderator_nudge_fired=False,
            per_speaker={
                n: {
                    "is_listen": r.is_listen,
                    "text": r.text,
                    "end_of_turn": r.end_of_turn,
                    "current_time": r.current_time,
                }
                for n, r in results.items()
            },
            rolled_back_this_tick=rolled_back,
        ))

    def _arm_break(self, name: str) -> None:
        if name not in self._break_armed:
            self._sessions[name].set_break()
            self._break_armed.add(name)

    def _tie_breaker(self, t: int) -> str:
        return self._rng.choice(list(self._sessions))

    @staticmethod
    def _one_sec(x: np.ndarray) -> np.ndarray:
        if x is None:
            return np.zeros(CHUNK_SAMPLES, np.float32)
        x = np.asarray(x, np.float32)
        if len(x) < CHUNK_SAMPLES:
            return np.pad(x, (0, CHUNK_SAMPLES - len(x)))
        return x[:CHUNK_SAMPLES]
