"""Contract test: MCPBackgroundReasoner.summarize() source_event_id traces to tool_call_completed.

Success criterion (T2 / numeric gate background_reasoner_summary_set_context_attribution_rate == 1.0):
  MemoryItem.source_event_id == tool_call_completed.event_id (invariant #3).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from companion_harness.background_reasoner import MCPBackgroundReasoner, ToolReasonerResult
from companion_harness.schemas import MemoryItem
from companion_harness.tool_router import ToolDispatchRequest


def _make_request() -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name="search_web",
        arguments={"query": "provenance"},
        caused_by=["upstream-1"],
        routing_hint="smart",
    )


def _collect_events() -> list:
    tool_mock = MagicMock()
    tool_mock.name = "search_web"
    tools_result = MagicMock()
    tools_result.tools = [tool_mock]

    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=tools_result)
    session.call_tool = AsyncMock(return_value=MagicMock())

    @asynccontextmanager
    async def _fake_stdio(_p):
        yield (MagicMock(), MagicMock())

    @asynccontextmanager
    async def _fake_session(_r, _w):
        yield session

    events = []

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
                session_id="sess-prov",
            )
            async for evt in reasoner.select_and_call(_make_request()):
                events.append(evt)
        finally:
            mcp.client.stdio.stdio_client = orig_sc
            mcp.StdioServerParameters = orig_sp
            mcp.ClientSession = orig_cs

    asyncio.run(_inner())
    return events


def test_summarize_source_event_id_traces_to_tool_call_completed() -> None:
    """MemoryItem.source_event_id == tool_call_completed.event_id (invariant #3)."""
    events = _collect_events()

    completed = next(
        (e for e in events if e.event_type == "tool_call_completed"), None
    )
    assert completed is not None, "No tool_call_completed event emitted"

    reasoner = MCPBackgroundReasoner(session_id="sess-prov", mcp_server_url="stdio://fake")
    result = ToolReasonerResult(
        tool_call_id=completed.payload_inline["tool_call_id"],
        tool_name="search_web",
        summary_text="Found relevant results.",
        caused_by=[completed.event_id],
    )

    items = reasoner.summarize(result)

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, MemoryItem)
    assert item.source_event_id == completed.event_id
    assert item.user_visible_summary.source_event_ids[0] == completed.event_id
    assert item.store == "episodic"
    assert item.valid_to is None
    assert item.superseded_by is None
