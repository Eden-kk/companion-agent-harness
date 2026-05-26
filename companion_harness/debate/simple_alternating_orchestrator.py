"""Simple turn-taking debate; no barge-in. Each turn is capped at `max_turn_ticks` and the
system prompt asks for ONE point in 1-2 sentences. KV cache stays continuous across turns by
design — see plan-two-minicpm-debate.md."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES

SILENCE = np.zeros(CHUNK_SAMPLES, np.float32)


@dataclass
class TurnRecord:
    turn_idx: int
    speaker: str
    text: str
    ticks_consumed: int
    natural_eot: bool
    per_tick_texts: list[str]


@dataclass
class SimpleDebateTrace:
    motion: str
    turns: list[TurnRecord]
    total_ticks: int
    transcript_terminated_by_max_turns: bool


class SimpleAlternatingOrchestrator:
    def __init__(
        self,
        motion: str,
        sessions: dict[str, Any],
        *,
        first_speaker: str = "A",
        max_turn_ticks: int = 4,
        total_turns: int = 8,
        listen_prob_scale_speaking: float = 0.5,
        listen_prob_scale_listening: float = 10.0,
        moderator_seed_audio: np.ndarray | None = None,
        turn_signal_text: str = "Your turn — make ONE clear point in 1-2 sentences, then stop.",
    ) -> None:
        self._motion = motion
        self._sessions = dict(sessions)
        self._first_speaker = first_speaker
        self._max_turn_ticks = max_turn_ticks
        self._total_turns = total_turns
        self._lps_speaking = listen_prob_scale_speaking
        self._lps_listening = listen_prob_scale_listening
        self._seed = moderator_seed_audio if moderator_seed_audio is not None else SILENCE.copy()
        self._turn_signal = turn_signal_text

    def run(self) -> SimpleDebateTrace:
        names = list(self._sessions)
        speaker = self._first_speaker
        listener = next(n for n in names if n != speaker)

        last_audio: dict[str, np.ndarray] = {n: self._seed.copy() for n in names}
        turns: list[TurnRecord] = []
        total_ticks = 0

        for turn_idx in range(self._total_turns):
            chunks_this_turn: list[str] = []
            natural_eot = False
            ticks_this_turn = 0

            for tick in range(self._max_turn_ticks):
                signal = [self._turn_signal] if tick == 0 else None
                self._sessions[speaker].prefill(last_audio[speaker], text_list=signal)
                self._sessions[listener].prefill(last_audio[listener])

                r_s = self._sessions[speaker].generate(listen_prob_scale=self._lps_speaking)

                if r_s.text:
                    chunks_this_turn.append(r_s.text)

                speaker_audio = r_s.audio_waveform if not r_s.is_listen else SILENCE.copy()
                last_audio[listener] = speaker_audio
                last_audio[speaker] = SILENCE.copy()

                ticks_this_turn += 1
                total_ticks += 1

                if not r_s.is_listen and r_s.end_of_turn:
                    natural_eot = True
                    break

            turns.append(TurnRecord(
                turn_idx=turn_idx,
                speaker=speaker,
                text=" ".join(c for c in chunks_this_turn if c).strip(),
                ticks_consumed=ticks_this_turn,
                natural_eot=natural_eot,
                per_tick_texts=chunks_this_turn,
            ))

            speaker, listener = listener, speaker

        return SimpleDebateTrace(
            motion=self._motion,
            turns=turns,
            total_ticks=total_ticks,
            transcript_terminated_by_max_turns=(len(turns) == self._total_turns),
        )
