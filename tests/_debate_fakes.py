"""Shared in-memory fakes for the debate/* test suite."""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES, DuplexTickResult


@dataclass
class ScriptedTick:
    is_listen: bool
    text: str = ""
    audio_waveform: np.ndarray | None = None
    end_of_turn: bool = False


class ScriptedDuplexSession:
    """Fake MiniCPMDuplexSession driven by a pre-scripted tick list.

    Break semantics: when set_break() is called, the *next* generate() returns
    is_listen=True, text="", audio=zeros, end_of_turn=True, then auto-clears.
    """

    def __init__(self, ticks: list[ScriptedTick], *, name: str = "fake") -> None:
        self._ticks = list(ticks)
        self._idx = 0
        self._break_set = False
        self._snapshot_saved = False
        self.name = name
        self.prefill_history: list[np.ndarray] = []
        self.prefill_text_history: list[list[str] | None] = []
        self.set_break_calls: list[int] = []

    def prefill(self, audio_1s: np.ndarray, *, text_list: list[str] | None = None) -> None:
        self.prefill_history.append(np.asarray(audio_1s, np.float32).copy())
        self.prefill_text_history.append(text_list)

    def generate(self, *, listen_prob_scale: float) -> DuplexTickResult:
        import time

        if self._break_set:
            self._break_set = False
            self._idx += 1
            return DuplexTickResult(
                is_listen=True,
                text="",
                audio_waveform=np.zeros(CHUNK_SAMPLES, dtype=np.float32),
                end_of_turn=True,
                current_time=time.monotonic(),
            )
        self._snapshot_saved = True
        tick = self._ticks[self._idx % len(self._ticks)]
        self._idx += 1
        wf = tick.audio_waveform if tick.audio_waveform is not None else np.zeros(CHUNK_SAMPLES, dtype=np.float32)
        return DuplexTickResult(
            is_listen=tick.is_listen,
            text=tick.text,
            audio_waveform=wf.copy(),
            end_of_turn=tick.end_of_turn,
            current_time=time.monotonic(),
        )

    def restore_snapshot(self) -> bool:
        if self._snapshot_saved:
            self._snapshot_saved = False
            return True
        return False

    def has_snapshot(self) -> bool:
        return self._snapshot_saved

    def set_break(self) -> None:
        self._break_set = True
        self.set_break_calls.append(self._idx)

    def clear_break(self) -> None:
        self._break_set = False

    def is_break_set(self) -> bool:
        return self._break_set
