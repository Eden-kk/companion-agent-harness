"""Contract test: MCPBackgroundReasoner raises BackgroundReasonerBudgetExhausted on wall-clock.

Success criterion (T3):
  test_wall_clock_budget_exhausted_raises: exception raised with budget_kind == "wall_clock"
  and terminal event is tool_call_cancelled.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from companion_harness.background_reasoner import (
    BackgroundReasonerBudgetExhausted,
    MCPBackgroundReasoner,
)
from companion_harness.tool_router import ToolDispatchRequest


def _make_request() -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name="slow_tool",
        arguments={},
        caused_by=["upstream-1"],
        routing_hint="smart",
    )


def test_wall_clock_budget_exhausted_raises() -> None:
    """budget_wall_clock_s=0.001 with a mock that advances time → BudgetExhausted(wall_clock)."""
    tool_mock = MagicMock()
    tool_mock.name = "slow_tool"
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

    # Use a time.monotonic side_effect that jumps by 10s after the first call
    _call_count = [0]
    _real_monotonic = time.monotonic
    def _fast_monotonic():
        _call_count[0] += 1
        if _call_count[0] <= 2:
            return 0.0
        return 100.0  # 100 seconds elapsed — well over any budget

    events = []
    exc_caught = []

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
                session_id="sess-budget-wc",
                budget_wall_clock_s=0.001,  # 1ms — will be exceeded by the mocked time
            )
            with patch("companion_harness.background_reasoner.time") as mock_time:
                mock_time.monotonic = _fast_monotonic
                try:
                    async for evt in reasoner.select_and_call(_make_request()):
                        events.append(evt)
                except BackgroundReasonerBudgetExhausted as exc:
                    exc_caught.append(exc)
        finally:
            mcp.client.stdio.stdio_client = orig_sc
            mcp.StdioServerParameters = orig_sp
            mcp.ClientSession = orig_cs

    asyncio.run(_inner())

    assert len(exc_caught) == 1, "Expected BackgroundReasonerBudgetExhausted to be raised"
    exc = exc_caught[0]
    assert exc.budget_kind == "wall_clock"
    assert exc.limit == 0.001
    assert exc.observed > exc.limit

    # Terminal event must be tool_call_cancelled
    event_types = [e.event_type for e in events]
    assert "tool_call_cancelled" in event_types
    assert "tool_call_completed" not in event_types
