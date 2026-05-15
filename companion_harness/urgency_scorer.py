"""UrgencyScorer — Protocol + null stub (v0.1j Task 10).

The real safety-risk classifier is unspecified; see issue #171.
This module establishes the seam so the live pipeline and tests can
reference a typed interface today. The stub returns 0.0 unconditionally;
it will be swapped for a real safety-risk classifier when #171 is resolved.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["UrgencyScorer", "_NullUrgencyScorer"]


@runtime_checkable
class UrgencyScorer(Protocol):
    def score(self, transcript: str, audio: bytes | None) -> float:
        """Return urgency/safety-risk score in [0.0, 1.0]. Higher = more urgent."""
        ...


class _NullUrgencyScorer:
    def score(self, transcript: str, audio: bytes | None) -> float:
        return 0.0  # UNAVAILABLE: #171 — real safety-risk classifier pending model selection
