"""Contract test: MCPBackgroundReasoner emits signal_producer_fallback on unknown_mcp_tool.

Success criterion (T2):
  test_unknown_mcp_tool_emits_signal_producer_fallback passes.
  When allowed_tools is non-None and MCP returns a tool not in the set,
  one signal_producer_fallback event fires, terminal is tool_call_cancelled.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

from companion_harness.background_reasoner import MCPBackgroundReasoner
from companion_harness.tool_router import ToolDispatchRequest


def _make_request(tool_name: str = "definitely_not_registered") -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name=tool_name,
        arguments={},
        caused_by=["upstream-evt-1"],
        routing_hint="smart",
    )


def _run_with_mcp_tool(mcp_tool_name: str, allowed_tools: "set[str] | None") -> list:
    tool_mock = MagicMock()
    tool_mock.name = mcp_tool_name
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
                session_id="sess-unknown",
                allowed_tools=allowed_tools,
            )
            request = _make_request(mcp_tool_name)
            async for evt in reasoner.select_and_call(request):
                events.append(evt)
        finally:
            mcp.client.stdio.stdio_client = orig_sc
            mcp.StdioServerParameters = orig_sp
            mcp.ClientSession = orig_cs

    asyncio.run(_inner())
    return events


def test_unknown_mcp_tool_emits_signal_producer_fallback() -> None:
    """allowed_tools non-None, MCP returns tool not in set → fallback + cancelled."""
    events = _run_with_mcp_tool(
        mcp_tool_name="definitely_not_registered",
        allowed_tools={"allowed_tool_only"},
    )

    event_types = [e.event_type for e in events]
    assert "signal_producer_fallback" in event_types
    assert "tool_call_cancelled" in event_types
    # No tool_call_completed (no successful dispatch)
    assert "tool_call_completed" not in event_types

    fb = next(e for e in events if e.event_type == "signal_producer_fallback")
    assert fb.payload_inline["primary_producer"] == "mcp_background_reasoner"
    assert fb.payload_inline["fallback_producer"] == "tool_call_cancelled"
    assert fb.payload_inline["reason"] == "unknown_mcp_tool"

    cancelled = next(e for e in events if e.event_type == "tool_call_cancelled")
    assert fb.event_id in cancelled.caused_by


def test_allowed_tools_none_trusts_mcp_unconditionally() -> None:
    """When allowed_tools=None, no fallback event fires even for unknown tool names."""
    events = _run_with_mcp_tool(
        mcp_tool_name="definitely_not_registered",
        allowed_tools=None,  # unconditional trust
    )

    event_types = [e.event_type for e in events]
    # No fallback — MCP is trusted unconditionally per OQ-2.3
    assert "signal_producer_fallback" not in event_types
    assert "tool_call_completed" in event_types
