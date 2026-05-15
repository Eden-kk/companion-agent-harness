"""ToolRouter Protocol + dispatch types (v0.1f Task 2 — Stage 5).

Defines the seam between the orchestrator and any tool-routing backend.
At v0.1f the only concrete implementation is FakeToolRouter (in-process,
deterministic).  Real MCP integration is deferred to v0.1g (OQ-1).

OQ-11 (local_only behaviour): a ToolRouter operating in `local_only`
privacy mode MUST hard-raise NotImplementedError on `dispatch()`.
This is a behavioural contract on concrete implementations, not enforceable
on the Protocol surface.

Anchor 2 ordering invariant (closes via caused_by[]):
    tool_call_requested
      → tool_call_dispatched
        → tool_progress_event*   (zero or more)
          → (tool_call_completed | tool_call_cancelled)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from companion_harness.schemas import Event

__all__ = ["ToolDispatchRequest", "ToolDispatchResult", "ToolRouter"]


@dataclass(frozen=True)
class ToolDispatchRequest:
    """Input to ToolRouter.dispatch()."""
    tool_name:     str
    arguments:     dict
    caused_by:     list[str]
    routing_hint:  Literal["fast", "smart"] | None = None


@dataclass(frozen=True)
class ToolDispatchResult:
    """Output of ToolRouter.dispatch()."""
    tool_call_id:  str
    final_status:  Literal["completed", "cancelled"]
    events:        list[Event]


@runtime_checkable
class ToolRouter(Protocol):
    """Adapter seam for tool routing (Anchor 3; OQ-1; OQ-6; OQ-11).

    Concrete implementations must hard-raise NotImplementedError on
    dispatch() when operating in `local_only` privacy mode (OQ-11).
    """

    async def dispatch(self, request: ToolDispatchRequest) -> ToolDispatchResult: ...

    async def cancel(self, tool_call_id: str) -> None: ...
