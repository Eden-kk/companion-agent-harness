"""DiarizationAdapter Protocol + DiarizationFrame dataclass + _NullDiarizationAdapter.

v0.2b T1 — Protocol foundation for speaker diarization (Anchor 2, Anchor 6).

Cross-session speaker recognition is NOT supported (OQ-A / roadmap-v0.2-draft.md
§Out of scope).  The per-session embedding registry is in-memory only; session
end discards it.

Event types introduced here (event-schema entries in v0_1g_event_schema.py):
  diarization_frame_produced  — per non-trivial frame (speaker_id is not None,
                                muted=False).
  speaker_continuity_anchor   — once per wake-word confirmation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "DiarizationFrame",
    "DiarizationAdapter",
    "_NullDiarizationAdapter",
]


@dataclass(frozen=True)
class DiarizationFrame:
    """Output of one DiarizationAdapter.process_chunk() call."""

    speaker_id: str | None
    confidence: float
    is_new_speaker: bool


@runtime_checkable
class DiarizationAdapter(Protocol):
    """Speaker-diarization adapter — one call per audio chunk.

    `muted` is a per-call parameter (not a setter / side-channel) so the
    adapter is testable as a pure function and replay-deterministic (Anchor 2).
    Callers read `audio_output_controller.is_synthesizing` at chunk-dispatch
    time and pass it as `muted`.

    Returns `DiarizationFrame`.  When `muted=True`, implementations MUST
    return `DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)`
    without updating internal state and without emitting events (Anchor 3).
    """

    def process_chunk(
        self,
        audio_bytes: bytes,
        ts_mono_ms: int,
        muted: bool,
        raw_audio_chunk_event_id: str = "",
    ) -> DiarizationFrame: ...


class _NullDiarizationAdapter:
    """Null implementation of DiarizationAdapter — always returns empty frame.

    Active when --enable-diarization is absent (default OFF per roadmap
    Anchor 3) so Tier-B replay vs v0.1k fixtures is bit-identical when the
    flag is absent.
    """

    def process_chunk(
        self,
        audio_bytes: bytes,
        ts_mono_ms: int,
        muted: bool,
        raw_audio_chunk_event_id: str = "",
    ) -> DiarizationFrame:
        return DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)
