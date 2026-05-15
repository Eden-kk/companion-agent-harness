"""FakeToolRouter — deterministic in-process ToolRouter (v0.1f Task 2).

Emits the full Anchor 2 event chain for every dispatch():
    tool_call_requested → tool_call_dispatched → tool_progress_event* →
    (tool_call_completed | tool_call_cancelled)

All events carry proper caused_by[] closure (invariant #1, OQ-2).
No model SDK imports (adapter-first invariant).

cancel() causes an in-flight dispatch to emit tool_call_cancelled instead
of tool_call_completed.  If no dispatch with that tool_call_id is running,
cancel() is a no-op (idempotent).

OQ-11: if privacy_mode="local_only" is passed to the constructor, dispatch()
raises NotImplementedError on every call.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Literal

from companion_harness.schemas import Event
from companion_harness.tool_progress import ProgressStage
from companion_harness.tool_router import ToolDispatchRequest, ToolDispatchResult

__all__ = ["FakeToolRouter"]

_SCHEMA_VERSION = "0.1"
_SOURCE = "fake_tool_router"

_PROGRESS_STAGE_ORDER: list[ProgressStage] = [
    "started", "scanning", "aggregating", "completed",
]


class FakeToolRouter:
    """Deterministic in-process ToolRouter for contract tests.

    Constructor args:
      session_id        — used as the prefix for all event_ids.
      progress_stages   — sequence of ProgressStage labels to emit as
                          tool_progress_event before the terminal event.
                          Defaults to ["started", "scanning", "aggregating"].
      privacy_mode      — if "local_only", dispatch() raises NotImplementedError
                          per OQ-11.  All other values are treated as normal.
    """

    def __init__(
        self,
        session_id: str = "test-session",
        progress_stages: list[ProgressStage] | None = None,
        privacy_mode: str = "normal",
    ) -> None:
        self._session_id = session_id
        self._progress_stages: list[ProgressStage] = (
            progress_stages if progress_stages is not None
            else ["started", "scanning", "aggregating"]
        )
        self._privacy_mode = privacy_mode
        self._seq = 0
        self._counter = 0
        # Maps tool_call_id → asyncio.Event; set() means cancel was requested.
        self._cancel_flags: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Protocol surface
    # ------------------------------------------------------------------

    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult:
        if self._privacy_mode == "local_only":
            raise NotImplementedError(
                "ToolRouter.dispatch() is unavailable in local_only privacy mode; "
                "see issue #96 (OQ-11)"
            )

        self._counter += 1
        tool_call_id = _deterministic_id(request.tool_name, self._counter)
        cancel_flag = asyncio.Event()
        self._cancel_flags[tool_call_id] = cancel_flag

        events: list[Event] = []

        # 1. tool_call_requested
        req_evt = self._make_event(
            event_type="tool_call_requested",
            caused_by=list(request.caused_by),
            tool_call_id=tool_call_id,
        )
        events.append(req_evt)

        # 2. tool_call_dispatched (Anchor 3: routing_tier)
        routing_tier: Literal["fast", "smart"] = request.routing_hint or "fast"
        dis_evt = self._make_event(
            event_type="tool_call_dispatched",
            caused_by=[req_evt.event_id],
            tool_call_id=tool_call_id,
            routing_tier=routing_tier,
        )
        events.append(dis_evt)

        # 3. tool_progress_event* — one per configured stage
        prev_id = dis_evt.event_id
        for stage in self._progress_stages:
            if cancel_flag.is_set():
                break
            prog_evt = self._make_event(
                event_type="tool_progress_event",
                caused_by=[prev_id],
                tool_call_id=tool_call_id,
                progress_stage=stage,
            )
            events.append(prog_evt)
            prev_id = prog_evt.event_id
            # Yield to event loop so cancel() can fire between stages.
            await asyncio.sleep(0)

        # 4. terminal event
        if cancel_flag.is_set():
            terminal_type = "tool_call_cancelled"
            final_status: Literal["completed", "cancelled"] = "cancelled"
        else:
            terminal_type = "tool_call_completed"
            final_status = "completed"

        terminal_evt = self._make_event(
            event_type=terminal_type,
            caused_by=[prev_id],
            tool_call_id=tool_call_id,
        )
        events.append(terminal_evt)
        del self._cancel_flags[tool_call_id]

        return ToolDispatchResult(
            tool_call_id=tool_call_id,
            final_status=final_status,
            events=events,
        )

    async def cancel(self, tool_call_id: str) -> None:
        flag = self._cancel_flags.get(tool_call_id)
        if flag is not None:
            flag.set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _make_event(
        self,
        event_type: str,
        caused_by: list[str],
        tool_call_id: str,
        routing_tier: Literal["fast", "smart"] | None = None,
        progress_stage: ProgressStage | None = None,
    ) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-ftr-{seq}-{now_ms}"
        extra = f"{tool_call_id}:{routing_tier}:{progress_stage}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}:{extra}".encode()
        ).hexdigest()[:16]
        inline: dict | None = {"tool_call_id": tool_call_id}
        if routing_tier is not None:
            inline["routing_tier"] = routing_tier
        if progress_stage is not None:
            inline["progress_stage"] = progress_stage
        return Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type=event_type,
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="tool_event",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="tool_call_audit_30d",
            payload_inline=inline,
        )


def _deterministic_id(tool_name: str, counter: int) -> str:
    """Stable tool_call_id: same inputs → same id (invariant #5)."""
    raw = f"{tool_name}:{counter}"
    return hashlib.sha256(raw.encode()).hexdigest()[:24]
