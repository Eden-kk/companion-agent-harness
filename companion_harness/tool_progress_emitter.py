"""ToolProgressEmitter — per-tool-call filler-budget state machine (v0.1f Task 4).

Anchor 4 replay-determinism contract: `evidence_at()` MUST derive
`ms_since_last_filler` from `timestamp_mono_ms` deltas between
`tool_progress_event`s in the event log, never from a live wall-clock read.

Budget caps (Anchor 4 / spec line 572–575):
  - max 2 fillers per call
  - 4 000 ms minimum gap between fillers
  - silence-wins-after-first-filler: once a filler is emitted, if the
    next opportunity arrives before the gap clears, `silence_won_already`
    is set and the remainder of the call is suppressed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, TYPE_CHECKING

from companion_harness.tool_progress import ProgressStage, ToolProgressEvidence

if TYPE_CHECKING:
    from companion_harness.schemas import Event

__all__ = ["ToolProgressEmitter"]

_MAX_FILLERS = 2
_MIN_GAP_MS = 4_000


@dataclass
class _CallState:
    fillers_emitted: int = 0
    last_filler_ts_mono_ms: int | None = None
    silence_won_already: bool = False


class ToolProgressEmitter:
    """Per-tool-call filler-budget state machine.

    One instance is typically held by the orchestrator for the duration of a
    session; state is keyed by `tool_call_id` so concurrent calls are isolated.
    """

    def __init__(self) -> None:
        self._calls: dict[str, _CallState] = {}

    def _state(self, tool_call_id: str) -> _CallState:
        if tool_call_id not in self._calls:
            self._calls[tool_call_id] = _CallState()
        return self._calls[tool_call_id]

    def should_emit_filler(self, tool_call_id: str, now_mono_ms: int) -> bool:
        """Return True iff the filler budget allows an emission right now."""
        s = self._state(tool_call_id)
        if s.silence_won_already:
            return False
        if s.fillers_emitted >= _MAX_FILLERS:
            return False
        if s.last_filler_ts_mono_ms is not None:
            gap = now_mono_ms - s.last_filler_ts_mono_ms
            if gap < _MIN_GAP_MS:
                if s.fillers_emitted >= 1:
                    s.silence_won_already = True
                return False
        return True

    def record_filler(self, tool_call_id: str, ts_mono_ms: int) -> None:
        """Record that a filler was emitted at `ts_mono_ms`."""
        s = self._state(tool_call_id)
        s.fillers_emitted += 1
        s.last_filler_ts_mono_ms = ts_mono_ms

    def evidence_at(
        self,
        tool_call_id: str,
        now_mono_ms: int,
        event_log: Iterable[Event],
    ) -> ToolProgressEvidence:
        """Pure derivation of ToolProgressEvidence from the event log.

        Derives `ms_since_last_filler` from `timestamp_mono_ms` deltas between
        `tool_progress_event`s in the provided iterable.  Never reads a wall clock.
        Replay-deterministic: same `event_log` → same output (invariant #5).
        """
        s = self._state(tool_call_id)

        last_progress_ts: int | None = None
        latest_stage: ProgressStage = "started"
        for evt in event_log:
            payload = getattr(evt, "payload_inline", None) or {}
            if not isinstance(payload, dict):
                continue
            if payload.get("tool_call_id") != tool_call_id:
                continue
            evt_type = getattr(evt, "event_type", None)
            if evt_type == "tool_progress_event":
                ts = getattr(evt, "timestamp_mono_ms", None)
                if ts is not None:
                    last_progress_ts = ts
                stage = payload.get("progress_stage")
                if stage is not None:
                    latest_stage = stage

        if last_progress_ts is not None:
            ms_since = now_mono_ms - last_progress_ts
        else:
            ms_since = now_mono_ms

        return ToolProgressEvidence(
            progress_stage=latest_stage,
            fillers_emitted_so_far=s.fillers_emitted,
            ms_since_last_filler=ms_since,
            silence_won_already=s.silence_won_already,
        )
