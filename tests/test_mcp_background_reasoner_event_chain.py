"""Contract test: MCPBackgroundReasoner emits Anchor 2 event chain with routing_tier="smart".

Success criterion (T2):
  test_select_and_call_emits_anchor2_chain_with_smart_tier passes.
  All emitted tool_* events have non-empty caused_by[] (tool_call_caused_by_closure_rate == 1.0).
  tool_call_dispatched carries routing_tier="smart" (routing_tier_smart_attribution_rate == 1.0).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from companion_harness.background_reasoner import MCPBackgroundReasoner
from companion_harness.tool_router import ToolDispatchRequest


def _make_request() -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name="search_web",
        arguments={"query": "test"},
        caused_by=["upstream-evt-1"],
        routing_hint="smart",
    )


def _fake_mcp_session(tool_names: list[str] = None) -> AsyncMock:
    if tool_names is None:
        tool_names = ["search_web"]
    tools_result = MagicMock()
    tools_result.tools = [MagicMock(name=n) for n in tool_names]
    for t, n in zip(tools_result.tools, tool_names):
        t.name = n
    call_result = MagicMock()
    call_result.content = []
    session = AsyncMock()
    session.initialize = AsyncMock()
    session.list_tools = AsyncMock(return_value=tools_result)
    session.call_tool = AsyncMock(return_value=call_result)
    return session


def _build_mcp_patches(session: AsyncMock) -> tuple:
    """Return (stdio_client_patch, ClientSession_patch, StdioServerParameters_patch)."""
    @asynccontextmanager
    async def _fake_stdio_client(_params):
        yield (MagicMock(), MagicMock())

    @asynccontextmanager
    async def _fake_client_session(_r, _w):
        yield session

    return _fake_stdio_client, _fake_client_session, MagicMock


def _run_reasoner(reasoner: MCPBackgroundReasoner, request: ToolDispatchRequest) -> list:
    session = _fake_mcp_session()
    stdio_fn, session_cls, params_cls = _build_mcp_patches(session)
    events = []

    async def _inner():
        with (
            patch("mcp.client.stdio.stdio_client", stdio_fn),
            patch("mcp.StdioServerParameters", params_cls),
        ):
            # Patch inside the function's local import scope
            import mcp
            import mcp.client.stdio
            orig_sc = mcp.client.stdio.stdio_client
            orig_sp = mcp.StdioServerParameters
            orig_cs = mcp.ClientSession
            mcp.client.stdio.stdio_client = stdio_fn
            mcp.StdioServerParameters = params_cls
            mcp.ClientSession = session_cls
            try:
                async for evt in reasoner.select_and_call(request):
                    events.append(evt)
            finally:
                mcp.client.stdio.stdio_client = orig_sc
                mcp.StdioServerParameters = orig_sp
                mcp.ClientSession = orig_cs

    asyncio.run(_inner())
    return events


def test_select_and_call_emits_anchor2_chain_with_smart_tier() -> None:
    """Anchor 2 chain: requested → dispatched(smart) → progress(started) → completed."""
    session = _fake_mcp_session(["search_web"])
    stdio_fn, session_cls, params_cls = _build_mcp_patches(session)
    events = []

    async def _inner():
        import mcp
        import mcp.client.stdio
        orig_sc = mcp.client.stdio.stdio_client
        orig_sp = mcp.StdioServerParameters
        orig_cs = mcp.ClientSession
        mcp.client.stdio.stdio_client = stdio_fn
        mcp.StdioServerParameters = params_cls
        mcp.ClientSession = session_cls
        try:
            reasoner = MCPBackgroundReasoner(
                mcp_server_url="stdio://fake",
                session_id="sess-mcp-1",
            )
            async for evt in reasoner.select_and_call(_make_request()):
                events.append(evt)
        finally:
            mcp.client.stdio.stdio_client = orig_sc
            mcp.StdioServerParameters = orig_sp
            mcp.ClientSession = orig_cs

    asyncio.run(_inner())

    event_types = [e.event_type for e in events]
    assert "tool_call_requested" in event_types
    assert "tool_call_dispatched" in event_types
    assert "tool_call_completed" in event_types

    # routing_tier="smart" on dispatched (Anchor A3)
    dispatched = next(e for e in events if e.event_type == "tool_call_dispatched")
    assert dispatched.payload_inline is not None
    assert dispatched.payload_inline.get("routing_tier") == "smart"

    # All tool_call_* events have non-empty caused_by[] (tool_call_caused_by_closure_rate == 1.0)
    for evt in events:
        if evt.event_type.startswith("tool_call"):
            assert evt.caused_by, f"{evt.event_type} has empty caused_by[]"

    # source attribution on non-fallback events
    for evt in events:
        if evt.event_type != "signal_producer_fallback":
            assert evt.source == "mcp_background_reasoner"

    # session_id propagates
    for evt in events:
        assert evt.session_id == "sess-mcp-1"
