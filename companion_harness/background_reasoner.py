"""BackgroundReasoner Protocol + FakeBackgroundReasoner (v0.1f Task 8 — Stage 5).
MCPBackgroundReasoner added in v0.2a Task 2.

Smart-path reasoner: given a ToolDispatchRequest, select and call tools
asynchronously and yield Events as they arrive; then summarize results
into MemoryItems for context injection via set_context() (OQ-7 / OQ-12).

No recursive ToolRouter calls at v0.1f (deferred to v0.1g per OQ-1).
No model SDK imports (adapter-first invariant) — mcp SDK imported inside
MCPBackgroundReasoner only.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, runtime_checkable

from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.tool_progress import ProgressStage
from companion_harness.tool_router import ToolDispatchRequest

__all__ = [
    "ToolReasonerResult",
    "BackgroundReasoner",
    "FakeBackgroundReasoner",
    "MCPBackgroundReasoner",
    "BackgroundReasonerBudgetExhausted",
]


class BackgroundReasonerBudgetExhausted(RuntimeError):
    """Raised when wall-clock or step-count budget is exceeded."""

    def __init__(
        self,
        budget_kind: str,        # 'wall_clock' | 'step_count'
        limit: int | float,
        observed: int | float,
    ) -> None:
        super().__init__(f"{budget_kind} budget exceeded: {observed} > {limit}")
        self.budget_kind = budget_kind
        self.limit = limit
        self.observed = observed

_SCHEMA_VERSION = "0.1"
_SOURCE = "fake_background_reasoner"


@dataclass(frozen=True)
class ToolReasonerResult:
    """Aggregated output of a BackgroundReasoner select_and_call() pass."""
    tool_call_id: str
    tool_name:    str
    summary_text: str
    caused_by:    list[str]


@runtime_checkable
class BackgroundReasoner(Protocol):
    """Adapter seam for the smart-path background reasoner (OQ-12).

    Callers subscribe via asyncio queue per OQ-12; the orchestrator
    forwards yielded Events to EventLogger.log() (invariant #10) and
    calls set_context() with summarize() results (OQ-7).
    """

    def select_and_call(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]: ...

    def summarize(self, results: ToolReasonerResult) -> list[MemoryItem]: ...


class FakeBackgroundReasoner:
    """Deterministic in-process BackgroundReasoner for contract tests.

    Emits two Events per select_and_call(): a reasoning_started and a
    reasoning_completed.  summarize() returns a single MemoryItem whose
    content echoes the result summary_text.

    No recursive ToolRouter calls (v0.1f scope per OQ-1).
    """

    def __init__(self, session_id: str = "test-session") -> None:
        self._session_id = session_id
        self._seq = 0

    def select_and_call(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]:
        return self._generate_events(request)

    async def _generate_events(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]:
        yield self._make_event(
            "background_reasoning_started",
            caused_by=list(request.caused_by),
        )
        yield self._make_event(
            "background_reasoning_completed",
            caused_by=list(request.caused_by),
        )

    def summarize(self, results: ToolReasonerResult) -> list[MemoryItem]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            MemoryItem(
                item_id=f"br-{results.tool_call_id}",
                store="episodic",
                content={"text": results.summary_text, "tool_name": results.tool_name},
                source_event_id=results.caused_by[0] if results.caused_by else "",
                created_at=now,
                last_confirmed_at=now,
                confidence=1.0,
                salience=0.5,
                privacy_level="user_content",
                mutability="system_revisable",
                valid_from=now,
                valid_to=None,
                superseded_by=None,
                user_visible_summary=SensitiveField(
                    retention_policy_id="ep_default_30d",
                    value=results.summary_text,
                    sensitivity="sensitive",
                    source_event_ids=list(results.caused_by),
                ),
            )
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _make_event(self, event_type: str, caused_by: list[str]) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-fbr-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}".encode()
        ).hexdigest()[:16]
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
            retention_policy_id="tool_result_default",
        )


_MCP_SOURCE = "mcp_background_reasoner"

# Map MCP progress message substrings → locked ProgressStage alphabet.
def _map_mcp_stage(label: str) -> ProgressStage | None:
    """Map an MCP free-text stage label to a locked ProgressStage; None = unknown."""
    low = label.lower()
    if any(k in low for k in ("scan", "search")):
        return "scanning"
    if any(k in low for k in ("aggregate", "summari")):
        return "aggregating"
    return None


class MCPBackgroundReasoner:
    """MCP-primary BackgroundReasoner (v0.2a Task 2 — Anchor A1).

    Wraps the upstream `mcp` Python SDK (>=1.6.0). SDK import lives inside
    _generate_events; callers that never instantiate this class pay no import cost.

    Budget enforcement (Anchor A4): wall-clock and step-count budgets are
    inline per-instance counters; no base class. FakeBackgroundReasoner is
    unchanged.

    allowed_tools (OQ-2.3): when None, every MCP-returned tool is accepted
    unconditionally. When a set, tools not in it emit signal_producer_fallback
    + tool_call_cancelled and return without raising.
    """

    def __init__(
        self,
        mcp_server_url: str,
        session_id: str = "test-session",
        budget_wall_clock_s: float = 30.0,
        budget_step_count: int = 8,
        allowed_tools: "set[str] | None" = None,
    ) -> None:
        self._mcp_server_url = mcp_server_url
        self._session_id = session_id
        self._budget_wall_clock_s = budget_wall_clock_s
        self._budget_step_count = budget_step_count
        self._allowed_tools = allowed_tools
        self._seq = 0

    def select_and_call(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]:
        return self._generate_events(request)

    async def _generate_events(
        self, request: ToolDispatchRequest
    ) -> AsyncIterator[Event]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        start_mono_ms = int(time.monotonic() * 1000)
        steps_taken = 0
        last_event_id: str = request.caused_by[0] if request.caused_by else ""

        # --- tool_call_requested ---
        tool_call_id = hashlib.sha256(
            f"{request.tool_name}:{start_mono_ms}".encode()
        ).hexdigest()[:24]
        req_evt = self._make_event(
            "tool_call_requested",
            caused_by=list(request.caused_by),
            tool_call_id=tool_call_id,
        )
        last_event_id = req_evt.event_id
        yield req_evt

        # Connect to MCP server and list tools.
        server_params = StdioServerParameters(command=self._mcp_server_url)
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools_result = await session.list_tools()
                available = {t.name for t in tools_result.tools}

                # Find the best tool for this request.
                tool_name = request.tool_name if request.tool_name in available else (
                    next(iter(available)) if available else None
                )

                # Unknown-tool check (OQ-2.3): only when allowed_tools is non-None.
                if (
                    tool_name is not None
                    and self._allowed_tools is not None
                    and tool_name not in self._allowed_tools
                ):
                    fallback_evt = self._make_signal_fallback_event(
                        caused_by=[last_event_id],
                        tool_call_id=tool_call_id,
                        primary_producer=_MCP_SOURCE,
                        fallback_producer="tool_call_cancelled",
                        reason="unknown_mcp_tool",
                    )
                    yield fallback_evt
                    cancel_evt = self._make_event(
                        "tool_call_cancelled",
                        caused_by=[fallback_evt.event_id],
                        tool_call_id=tool_call_id,
                    )
                    yield cancel_evt
                    return

                if tool_name is None:
                    cancel_evt = self._make_event(
                        "tool_call_cancelled",
                        caused_by=[last_event_id],
                        tool_call_id=tool_call_id,
                    )
                    yield cancel_evt
                    return

                # --- tool_call_dispatched (routing_tier="smart") ---
                dis_evt = self._make_event(
                    "tool_call_dispatched",
                    caused_by=[req_evt.event_id],
                    tool_call_id=tool_call_id,
                    routing_tier="smart",
                )
                last_event_id = dis_evt.event_id
                yield dis_evt

                # --- started progress ---
                prog_evt = self._make_event(
                    "tool_progress_event",
                    caused_by=[last_event_id],
                    tool_call_id=tool_call_id,
                    progress_stage="started",
                )
                last_event_id = prog_evt.event_id
                yield prog_evt
                steps_taken += 1

                # Budget check before dispatch.
                elapsed_s = (int(time.monotonic() * 1000) - start_mono_ms) / 1000
                if elapsed_s > self._budget_wall_clock_s or steps_taken > self._budget_step_count:
                    cancel_evt = self._make_event(
                        "tool_call_cancelled",
                        caused_by=[last_event_id],
                        tool_call_id=tool_call_id,
                    )
                    yield cancel_evt
                    kind = "wall_clock" if elapsed_s > self._budget_wall_clock_s else "step_count"
                    limit = self._budget_wall_clock_s if kind == "wall_clock" else self._budget_step_count
                    observed = elapsed_s if kind == "wall_clock" else steps_taken
                    raise BackgroundReasonerBudgetExhausted(kind, limit, observed)

                # Collect progress events via callback.
                progress_events: list[Event] = []

                async def _on_progress(
                    progress: float,
                    total: "float | None",
                    message: "str | None",
                ) -> None:
                    nonlocal last_event_id
                    msg = message or ""
                    stage = _map_mcp_stage(msg)
                    if stage is None:
                        fb = self._make_signal_fallback_event(
                            caused_by=[last_event_id],
                            tool_call_id=tool_call_id,
                            primary_producer=_MCP_SOURCE,
                            fallback_producer="progress_stage_default",
                            reason="unknown_mcp_progress_stage",
                        )
                        progress_events.append(fb)
                        last_event_id = fb.event_id
                        stage = "scanning"
                    p = self._make_event(
                        "tool_progress_event",
                        caused_by=[last_event_id],
                        tool_call_id=tool_call_id,
                        progress_stage=stage,
                    )
                    progress_events.append(p)
                    last_event_id = p.event_id

                await session.call_tool(
                    tool_name,
                    request.arguments,
                    progress_callback=_on_progress,
                )

                # Yield collected progress events, enforcing budget after each.
                for p_evt in progress_events:
                    steps_taken += 1
                    elapsed_s = (int(time.monotonic() * 1000) - start_mono_ms) / 1000
                    if elapsed_s > self._budget_wall_clock_s or steps_taken > self._budget_step_count:
                        cancel_evt = self._make_event(
                            "tool_call_cancelled",
                            caused_by=[p_evt.event_id],
                            tool_call_id=tool_call_id,
                        )
                        yield cancel_evt
                        kind = "wall_clock" if elapsed_s > self._budget_wall_clock_s else "step_count"
                        limit = self._budget_wall_clock_s if kind == "wall_clock" else self._budget_step_count
                        observed = elapsed_s if kind == "wall_clock" else steps_taken
                        raise BackgroundReasonerBudgetExhausted(kind, limit, observed)
                    yield p_evt

        # --- tool_call_completed ---
        completed_evt = self._make_event(
            "tool_call_completed",
            caused_by=[last_event_id],
            tool_call_id=tool_call_id,
        )
        yield completed_evt

    def summarize(self, results: ToolReasonerResult) -> list[MemoryItem]:
        now = datetime.now(timezone.utc).isoformat()
        return [
            MemoryItem(
                item_id=f"mcpbr-{results.tool_call_id}",
                store="episodic",
                content={"text": results.summary_text, "tool_name": results.tool_name},
                source_event_id=results.caused_by[0] if results.caused_by else "",
                created_at=now,
                last_confirmed_at=now,
                confidence=1.0,
                salience=0.5,
                privacy_level="user_content",
                mutability="system_revisable",
                valid_from=now,
                valid_to=None,
                superseded_by=None,
                user_visible_summary=SensitiveField(
                    retention_policy_id="ep_default_30d",
                    value=results.summary_text,
                    sensitivity="sensitive",
                    source_event_ids=list(results.caused_by),
                ),
            )
        ]

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
        routing_tier: "Literal['fast', 'smart'] | None" = None,
        progress_stage: "ProgressStage | None" = None,
    ) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-mcpbr-{seq}-{now_ms}"
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
            source=_MCP_SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="tool_event",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="tool_call_audit_30d",
            payload_inline=inline,
        )

    def _make_signal_fallback_event(
        self,
        caused_by: list[str],
        tool_call_id: str,
        primary_producer: str,
        fallback_producer: str,
        reason: str,
    ) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-mcpbr-{seq}-{now_ms}"
        inline = {
            "tool_call_id": tool_call_id,
            "primary_producer": primary_producer,
            "fallback_producer": fallback_producer,
            "reason": reason,
        }
        payload_hash = hashlib.sha256(
            f"signal_producer_fallback:{event_id}:{reason}".encode()
        ).hexdigest()[:16]
        return Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="signal_producer_fallback",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_MCP_SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline=inline,
        )
