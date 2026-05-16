"""Contract test: ToolProgressEmitter.evidence_at() is bit-identical on recorded MCP events.

Success criterion (T5 / Anchor A2):
  evidence_at(tool_call_id, now_mono_ms, event_log) called twice on the same
  recorded event log returns byte-identical ToolProgressEvidence (invariant #5).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from companion_harness.background_reasoner import MCPBackgroundReasoner
from companion_harness.schemas import Event
from companion_harness.tool_progress_emitter import ToolProgressEmitter
from companion_harness.tool_router import ToolDispatchRequest


def _make_request() -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name="search_web",
        arguments={"query": "replay"},
        caused_by=["upstream-1"],
        routing_hint="smart",
    )


def _collect_mcp_events() -> list[Event]:
    """Run MCPBackgroundReasoner with a fake MCP server; return the event stream."""
    tool_mock = MagicMock()
    tool_mock.name = "search_web"
    tools_result = MagicMock()
    tools_result.tools = [tool_mock]

    async def _call_tool_with_progress(name, args, progress_callback=None, **kwargs):
        if progress_callback is not None:
            await progress_callback(0.5, 1.0, "scanning results")
            await progress_callback(1.0, 1.0, "aggregating data")
        return MagicMock()

    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=tools_result)
    session.call_tool = _call_tool_with_progress

    @asynccontextmanager
    async def _fake_stdio(_p):
        yield (MagicMock(), MagicMock())

    @asynccontextmanager
    async def _fake_session(_r, _w):
        yield session

    events: list[Event] = []

    async def _inner():
        import mcp
        import mcp.client.stdio
        orig_sc = mcp.client.stdio.stdio_client
        orig_sp = mcp.StdioServerParameters
        orig_cs = mcp.ClientSession
        mcp.client.stdio.stdio_client = _fake_stdio
        mcp.StdioServerParameters = MagicMock
        mcp.ClientSession = _fake_session
        try:
            reasoner = MCPBackgroundReasoner(
                mcp_server_url="stdio://fake",
                session_id="sess-replay",
            )
            async for evt in reasoner.select_and_call(_make_request()):
                events.append(evt)
        finally:
            mcp.client.stdio.stdio_client = orig_sc
            mcp.StdioServerParameters = orig_sp
            mcp.ClientSession = orig_cs

    asyncio.run(_inner())
    return events


def test_evidence_at_replay_from_recorded_events_is_bit_identical() -> None:
    """Two calls to evidence_at() on the same frozen event log → identical output."""
    recorded_events = _collect_mcp_events()

    # Find the tool_call_id from the dispatched event
    dispatched = next(e for e in recorded_events if e.event_type == "tool_call_dispatched")
    tool_call_id = dispatched.payload_inline["tool_call_id"]

    # Use a fixed now_mono_ms (replay must never read wall clock — invariant #5)
    last_progress = max(
        (e.timestamp_mono_ms for e in recorded_events if e.event_type == "tool_progress_event"),
        default=0,
    )
    now_mono_ms = last_progress + 5000  # 5 seconds after last progress

    emitter1 = ToolProgressEmitter()
    evidence_a = emitter1.evidence_at(tool_call_id, now_mono_ms, recorded_events)

    emitter2 = ToolProgressEmitter()
    evidence_b = emitter2.evidence_at(tool_call_id, now_mono_ms, recorded_events)

    # Bit-identical (invariant #5 / Anchor A2)
    assert asdict(evidence_a) == asdict(evidence_b), (
        f"evidence_at not deterministic:\n  first:  {evidence_a}\n  second: {evidence_b}"
    )

    # Sanity: ms_since_last_filler == now_mono_ms - last_progress_ts (Anchor A2 formula)
    assert evidence_a.ms_since_last_filler == 5000

    # progress_stage should reflect the last recorded stage
    assert evidence_a.progress_stage in ("scanning", "aggregating")
