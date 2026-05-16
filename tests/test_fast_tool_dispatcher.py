"""Tests for FastToolDispatcher concrete adapter (v0.1f Task 3).

Success criterion: pytest tests/test_fast_tool_dispatcher.py -v passes (5+ tests).
"""

from __future__ import annotations

import asyncio

import pytest

from companion_harness.fast_tool_dispatcher import FastToolDispatcher
from companion_harness.tool_router import ToolDispatchRequest, ToolRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_request(**kwargs) -> ToolDispatchRequest:
    defaults = dict(
        tool_name="weather",
        arguments={"city": "NYC"},
        caused_by=["upstream-evt-1"],
    )
    defaults.update(kwargs)
    return ToolDispatchRequest(**defaults)


def _make_dispatcher(**kwargs) -> FastToolDispatcher:
    tools = kwargs.pop("tools", {"weather": lambda city: f"Sunny in {city}", "calc": lambda x: x * 2})
    return FastToolDispatcher(tools=tools, session_id="s1", **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_dispatch_resolves_tool_by_name():
    """dispatch() calls the matching callable from the registry."""
    called_with: dict = {}

    def weather_fn(city):
        called_with["city"] = city
        return "Sunny"

    dispatcher = FastToolDispatcher(tools={"weather": weather_fn}, session_id="s1")
    result = _run(dispatcher.dispatch(_make_request(tool_name="weather", arguments={"city": "NYC"})))
    assert result.final_status == "completed"
    assert called_with == {"city": "NYC"}


def test_dispatch_emits_full_event_chain_with_fast_routing_tier():
    """dispatch() emits requested → dispatched → started progress → completed; routing_tier="fast"."""
    dispatcher = _make_dispatcher()
    result = _run(dispatcher.dispatch(_make_request()))

    types = [e.event_type for e in result.events]
    assert types[0] == "tool_call_requested"
    assert types[1] == "tool_call_dispatched"
    assert "tool_progress_event" in types
    assert types[-1] == "tool_call_completed"

    # Anchor 3: routing_tier must be "fast" on tool_call_dispatched
    dispatched = next(e for e in result.events if e.event_type == "tool_call_dispatched")
    assert dispatched.payload_inline is not None
    assert dispatched.payload_inline["routing_tier"] == "fast"

    # Progress event carries progress_stage="started"
    progress = next(e for e in result.events if e.event_type == "tool_progress_event")
    assert progress.payload_inline is not None
    assert progress.payload_inline["progress_stage"] == "started"

    # caused_by closure: each event (except first) cites its predecessor
    upstream = "upstream-evt-1"
    events = result.events
    assert upstream in events[0].caused_by
    for i in range(1, len(events)):
        assert events[i - 1].event_id in events[i].caused_by


def test_dispatch_unknown_tool_raises():
    """dispatch() raises KeyError for a tool_name not in the registry."""
    dispatcher = FastToolDispatcher(tools={"calc": lambda x: x}, session_id="s1")
    with pytest.raises(KeyError, match="unknown_tool"):
        _run(dispatcher.dispatch(_make_request(tool_name="unknown_tool")))


def test_cancel_during_dispatch_emits_tool_call_cancelled():
    """cancel() before the tool runs causes tool_call_cancelled in the event chain."""

    async def _run_cancel():
        async def slow_tool(city):
            await asyncio.sleep(10)

        dispatcher = FastToolDispatcher(
            tools={"weather": slow_tool}, session_id="s1"
        )
        dispatch_task = asyncio.create_task(
            dispatcher.dispatch(_make_request(tool_name="weather", arguments={"city": "NYC"}))
        )
        # Yield so the task registers its cancel_flag.
        await asyncio.sleep(0)
        assert len(dispatcher._cancel_flags) == 1
        tcid = next(iter(dispatcher._cancel_flags))
        await dispatcher.cancel(tcid)
        return await dispatch_task

    result = asyncio.run(_run_cancel())
    types = [e.event_type for e in result.events]
    assert "tool_call_cancelled" in types
    assert "tool_call_completed" not in types
    assert result.final_status == "cancelled"


def test_dispatch_local_only_raises_with_96_marker():
    """local_only privacy mode raises NotImplementedError citing issue #96 (OQ-11)."""
    dispatcher = FastToolDispatcher(
        tools={"weather": lambda city: "Sunny"},
        session_id="s1",
        privacy_mode="local_only",
    )
    with pytest.raises(NotImplementedError, match="#96"):
        _run(dispatcher.dispatch(_make_request()))


def test_fast_tool_dispatcher_satisfies_protocol():
    """FastToolDispatcher satisfies the ToolRouter Protocol isinstance check."""
    dispatcher = _make_dispatcher()
    assert isinstance(dispatcher, ToolRouter)
