"""Contract test: MCPBackgroundReasoner raises BackgroundReasonerBudgetExhausted on step count.

Success criterion (T3):
  test_step_count_budget_exhausted_raises: exception raised with budget_kind == "step_count"
  after the step limit is exceeded.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest

from companion_harness.background_reasoner import (
    BackgroundReasonerBudgetExhausted,
    MCPBackgroundReasoner,
)
from companion_harness.tool_router import ToolDispatchRequest


def _make_request() -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name="step_tool",
        arguments={},
        caused_by=["upstream-1"],
        routing_hint="smart",
    )


def test_step_count_budget_exhausted_raises() -> None:
    """budget_step_count=2 with enough progress events → BudgetExhausted(step_count).

    The reasoner starts counting steps at the started progress event (step 1).
    With budget_step_count=2, the 3rd step triggers the exception.
    The callback fires for each progress notification from call_tool.
    """
    tool_mock = MagicMock()
    tool_mock.name = "step_tool"
    tools_result = MagicMock()
    tools_result.tools = [tool_mock]

    # Simulate call_tool invoking the progress callback 5 times
    async def _call_tool_with_progress(
        name, args, progress_callback=None, **kwargs
    ):
        if progress_callback is not None:
            for i in range(5):
                await progress_callback(
                    progress=float(i),
                    total=5.0,
                    message=f"scanning step {i}",
                )
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
                session_id="sess-budget-sc",
                budget_step_count=2,  # started counts as step 1; 3rd step triggers
            )
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
    assert exc.budget_kind == "step_count"
    assert exc.limit == 2
    assert exc.observed > exc.limit

    event_types = [e.event_type for e in events]
    assert "tool_call_cancelled" in event_types
    assert "tool_call_completed" not in event_types
