"""AudioVisualConflictScorer — Protocol + null stub (v0.1j Task 6).

The real cross-modal scorer is unspecified; see issue #168.
This module establishes the seam so the live pipeline and tests can
reference a typed interface today. The stub returns 0.0 unconditionally;
it will be swapped for a real CLIP-audio-encoder variant (or similar)
when #168 is resolved.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["AudioVisualConflictScorer", "_NullAudioVisualConflictScorer"]


@runtime_checkable
class AudioVisualConflictScorer(Protocol):
    def score(self, audio: bytes, frame: bytes | None) -> float:
        """Return audio-visual conflict score in [0.0, 1.0]. Higher = more conflict."""
        ...


class _NullAudioVisualConflictScorer:
    def score(self, audio: bytes, frame: bytes | None) -> float:
        return 0.0  # UNAVAILABLE: #168 — real cross-modal conflict scorer pending spec decision
