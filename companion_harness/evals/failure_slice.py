"""No-op FailureSliceExtractor — A3's adapter uses this as its slicer."""

from __future__ import annotations

from companion_harness.evals.schemas import FailureSlice


class NoOpFailureSliceExtractor:
    def extract(self, case: object, replay_run: object, result: object) -> list[FailureSlice]:
        return []
