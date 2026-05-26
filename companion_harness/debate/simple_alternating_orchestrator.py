"""Simple turn-taking debate; no barge-in. Strict per-turn handoff:

- Only the current speaker's session is touched during the speaking phase.
- Each turn ends when the speaker emits 1-2 complete sentences (text ends in .!?)
  OR a natural end_of_turn, OR a hard tick cap is reached (safety only).
- Between turns, the just-finished speaker's accumulated 1 s audio chunks are
  replayed into the new speaker's prefill (1 chunk per call), with a text_list
  marker on the first chunk explicitly naming the audio as the opponent's so
  the new speaker doesn't confuse it with its own KV history.

KV cache stays continuous within each session by design; the new-turn marker is
what disambiguates self-vs-opponent in the listener's context."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES

SILENCE = np.zeros(CHUNK_SAMPLES, np.float32)

# A complete sentence ends with one of these.
_SENT_END = re.compile(r"[.!?]\s*$")


def _looks_complete(text: str) -> bool:
    return bool(_SENT_END.search(text.rstrip()))


def _count_sentences(text: str) -> int:
    # Count any sentence-ending punctuation cluster.
    return len(re.findall(r"[.!?]+", text))


@dataclass
class TurnRecord:
    turn_idx: int
    speaker: str
    text: str
    ticks_consumed: int
    natural_eot: bool
    ended_on_sentence: bool                # True if turn ended due to sentence-completion detection
    per_tick_texts: list[str] = field(default_factory=list)
    audio_chunks_count: int = 0            # how many 1 s audio chunks this turn produced (informational)


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
        max_turn_ticks: int = 12,
        total_turns: int = 8,
        max_sentences_per_turn: int = 2,
        listen_prob_scale_speaking: float = 0.5,
        moderator_seed_audio: np.ndarray | None = None,
        first_turn_signal: str = (
            "You are the proposition. Open the debate with ONE clear point in 1-2 complete sentences, then stop."
        ),
        handoff_signal_template: str = (
            'Your opponent ({opp}) just said: "{opp_text}". '
            "Respond directly to that point in 1-2 complete sentences, then stop."
        ),
    ) -> None:
        self._motion = motion
        self._sessions = dict(sessions)
        self._first_speaker = first_speaker
        self._max_turn_ticks = max_turn_ticks
        self._total_turns = total_turns
        self._max_sentences = max_sentences_per_turn
        self._lps_speaking = listen_prob_scale_speaking
        self._seed = moderator_seed_audio if moderator_seed_audio is not None else SILENCE.copy()
        self._first_signal = first_turn_signal
        self._handoff_template = handoff_signal_template

    def run(self) -> SimpleDebateTrace:
        names = list(self._sessions)
        speaker = self._first_speaker
        opponent = next(n for n in names if n != speaker)

        # Per-turn accumulated audio chunks for the previous speaker; replayed
        # into the next speaker's prefill at turn boundary.
        prev_speaker: str | None = None
        prev_audio_chunks: list[np.ndarray] = []
        prev_text: str = ""

        # Seed audio is only used for the very first turn's first prefill.
        first_turn_prefill_audio = self._seed.copy()

        turns: list[TurnRecord] = []
        total_ticks = 0

        for turn_idx in range(self._total_turns):
            # 1. HANDOFF — feed the new speaker its opponent's recent audio + text marker.
            if turn_idx == 0:
                # First turn: seed audio + opener signal.
                self._sessions[speaker].prefill(first_turn_prefill_audio, text_list=[self._first_signal])
            elif prev_audio_chunks:
                marker = self._handoff_template.format(opp=prev_speaker, opp_text=prev_text)
                # First chunk carries the text marker; subsequent chunks just deliver audio.
                self._sessions[speaker].prefill(prev_audio_chunks[0], text_list=[marker])
                for chunk in prev_audio_chunks[1:]:
                    self._sessions[speaker].prefill(chunk)
            else:
                # Prior speaker produced no audio (likely chose to listen all ticks).
                # Inject a short re-prompt so the new speaker doesn't also stall.
                fallback = (
                    f"Your opponent ({prev_speaker}) said nothing on their turn. "
                    "Make ONE clear point in 1-2 complete sentences and stop."
                )
                self._sessions[speaker].prefill(SILENCE, text_list=[fallback])

            # 2. SPEAKING LOOP — only the speaker generates. Listener is untouched.
            chunks_this_turn: list[str] = []
            audio_this_turn: list[np.ndarray] = []
            natural_eot = False
            ended_on_sentence = False
            ticks_this_turn = 0

            for tick in range(self._max_turn_ticks):
                # Speaker has no incoming audio mid-turn (it's holding the floor).
                if tick > 0:
                    self._sessions[speaker].prefill(SILENCE)
                r = self._sessions[speaker].generate(listen_prob_scale=self._lps_speaking)

                if r.text:
                    chunks_this_turn.append(r.text)
                if not r.is_listen and r.audio_waveform is not None:
                    audio_this_turn.append(np.asarray(r.audio_waveform, dtype=np.float32))

                ticks_this_turn += 1
                total_ticks += 1

                if not r.is_listen and r.end_of_turn:
                    natural_eot = True
                    break

                accumulated = " ".join(c for c in chunks_this_turn if c).strip()
                if (
                    _looks_complete(accumulated)
                    and _count_sentences(accumulated) >= 1
                    and _count_sentences(accumulated) <= self._max_sentences
                ):
                    ended_on_sentence = True
                    break
                if _count_sentences(accumulated) > self._max_sentences:
                    ended_on_sentence = True
                    break

            full_text = " ".join(c for c in chunks_this_turn if c).strip()
            turns.append(TurnRecord(
                turn_idx=turn_idx,
                speaker=speaker,
                text=full_text,
                ticks_consumed=ticks_this_turn,
                natural_eot=natural_eot,
                ended_on_sentence=ended_on_sentence,
                per_tick_texts=chunks_this_turn,
                audio_chunks_count=len(audio_this_turn),
            ))

            # 3. ROLE SWAP — stash this turn's audio + text for the next handoff.
            prev_speaker = speaker
            prev_audio_chunks = audio_this_turn
            prev_text = full_text
            speaker, opponent = opponent, speaker

        return SimpleDebateTrace(
            motion=self._motion,
            turns=turns,
            total_ticks=total_ticks,
            transcript_terminated_by_max_turns=(len(turns) == self._total_turns),
        )
