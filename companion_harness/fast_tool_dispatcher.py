"""FastToolDispatcher — concrete fast-path ToolRouter (v0.1f Task 3).

Resolves tool calls from a local registry (tool_name → callable), runs them
synchronously, and emits the full Anchor 2 event chain with routing_tier="fast":

    tool_call_requested → tool_call_dispatched → tool_progress_event* →
    (tool_call_completed | tool_call_cancelled)

All events carry proper caused_by[] closure (invariant #1).
No model SDK imports (adapter-first invariant).

OQ-11 / issue #96: if privacy_mode="local_only", dispatch() raises
NotImplementedError on every call.

Anchor 3: routing_tier is always "fast"; FastToolDispatcher does not support
the smart path.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timezone
from typing import Callable, Literal

from companion_harness.schemas import Event
from companion_harness.tool_progress import ProgressStage
from companion_harness.tool_router import ToolDispatchRequest, ToolDispatchResult

__all__ = ["FastToolDispatcher"]

_SCHEMA_VERSION = "0.1"
_SOURCE = "fast_tool_dispatcher"


class FastToolDispatcher:
    """Concrete fast-path ToolRouter backed by a local callable registry.

    Constructor args:
      tools         — dict mapping tool_name → callable (sync or async).
                      dispatch() raises KeyError for unknown names.
      session_id    — prefix for emitted event_ids.
      privacy_mode  — if "local_only", dispatch() raises NotImplementedError
                      per OQ-11 / issue #96.
    """

    def __init__(
        self,
        tools: dict[str, Callable],
        session_id: str = "test-session",
        privacy_mode: str = "normal",
    ) -> None:
        self._tools = tools
        self._session_id = session_id
        self._privacy_mode = privacy_mode
        self._seq = 0
        self._counter = 0
        self._cancel_flags: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # Protocol surface
    # ------------------------------------------------------------------

    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult:
        if self._privacy_mode == "local_only":
            raise NotImplementedError(
                "FastToolDispatcher.dispatch() is unavailable in local_only "
                "privacy mode; see issue #96 (OQ-11)"
            )

        if request.tool_name not in self._tools:
            raise KeyError(f"Unknown tool: {request.tool_name!r}")

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

        # 2. tool_call_dispatched (Anchor 3: routing_tier="fast")
        dis_evt = self._make_event(
            event_type="tool_call_dispatched",
            caused_by=[req_evt.event_id],
            tool_call_id=tool_call_id,
            routing_tier="fast",
        )
        events.append(dis_evt)

        # 3. started progress event
        prev_id = dis_evt.event_id
        if not cancel_flag.is_set():
            prog_evt = self._make_event(
                event_type="tool_progress_event",
                caused_by=[prev_id],
                tool_call_id=tool_call_id,
                progress_stage="started",
            )
            events.append(prog_evt)
            prev_id = prog_evt.event_id

        # Yield to event loop so cancel() can fire before the tool runs.
        await asyncio.sleep(0)

        # 4. Run the tool (if not cancelled)
        if not cancel_flag.is_set():
            fn = self._tools[request.tool_name]
            if asyncio.iscoroutinefunction(fn):
                await fn(**request.arguments)
            else:
                fn(**request.arguments)

        # 5. terminal event
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
        event_id = f"{self._session_id}-ftd-{seq}-{now_ms}"
        extra = f"{tool_call_id}:{routing_tier}:{progress_stage}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}:{extra}".encode()
        ).hexdigest()[:16]
        inline: dict = {"tool_call_id": tool_call_id}
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
