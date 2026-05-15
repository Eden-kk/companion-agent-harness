"""Tests for ToolRouter Protocol + FakeToolRouter (v0.1f Task 2).

Success criterion: pytest tests/test_tool_router_fake.py -v passes.

Covers:
  - happy-path dispatch emits requested / dispatched / completed
  - caused_by[] closes back through the chain to the upstream request
  - cancel() causes cancelled not completed
  - progress_stage sequence respects the locked alphabet order
  - tool_call_id is deterministic (same inputs → same id)
  - local_only raises NotImplementedError (OQ-11)
  - ToolRouter Protocol runtime check passes for FakeToolRouter
"""

from __future__ import annotations

import asyncio

import pytest

from companion_harness.fake_tool_router import FakeToolRouter, _deterministic_id
from companion_harness.tool_router import ToolDispatchRequest, ToolRouter
from companion_harness.tool_progress import ProgressStage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_request(**kwargs) -> ToolDispatchRequest:
    defaults = dict(
        tool_name="search",
        arguments={"q": "test"},
        caused_by=["upstream-evt-1"],
    )
    defaults.update(kwargs)
    return ToolDispatchRequest(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_fake_tool_router_dispatch_emits_requested_dispatched_completed():
    router = FakeToolRouter(session_id="s1", progress_stages=[])
    result = _run(router.dispatch(_make_request()))
    types = [e.event_type for e in result.events]
    assert types == ["tool_call_requested", "tool_call_dispatched", "tool_call_completed"]
    assert result.final_status == "completed"


def test_fake_tool_router_caused_by_closure():
    """Every event's caused_by[] closes back to the upstream request signal."""
    upstream = "user-utterance-evt-abc"
    router = FakeToolRouter(session_id="s1", progress_stages=["started", "scanning"])
    result = _run(router.dispatch(_make_request(caused_by=[upstream])))
    events = result.events

    # tool_call_requested cites the upstream signal
    assert upstream in events[0].caused_by

    # Each subsequent event cites its immediate predecessor
    for i in range(1, len(events)):
        assert events[i - 1].event_id in events[i].caused_by

    # Transitive closure: every event can trace back to the upstream signal
    # by following the chain.
    id_to_evt = {e.event_id: e for e in events}
    id_to_evt[upstream] = None  # sentinel

    def reaches_upstream(evt_id: str, visited: set) -> bool:
        if evt_id == upstream:
            return True
        if evt_id in visited or evt_id not in id_to_evt:
            return False
        visited.add(evt_id)
        evt = id_to_evt[evt_id]
        if evt is None:
            return False
        return any(reaches_upstream(p, visited) for p in evt.caused_by)

    for evt in events:
        assert reaches_upstream(evt.event_id, set()), (
            f"{evt.event_type} does not trace back to upstream signal"
        )


def test_fake_tool_router_cancel_emits_cancelled_not_completed():
    """cancel() during dispatch causes tool_call_cancelled, not tool_call_completed."""

    async def _run_cancel():
        router = FakeToolRouter(
            session_id="s1",
            progress_stages=["started", "scanning", "aggregating"],
        )
        dispatch_task = asyncio.create_task(router.dispatch(_make_request()))
        # Yield enough for dispatch to start and register the cancel flag.
        await asyncio.sleep(0)
        # Retrieve the in-flight tool_call_id from the cancel flags dict.
        assert len(router._cancel_flags) == 1
        tcid = next(iter(router._cancel_flags))
        await router.cancel(tcid)
        return await dispatch_task

    result = asyncio.run(_run_cancel())
    types = [e.event_type for e in result.events]
    assert "tool_call_cancelled" in types
    assert "tool_call_completed" not in types
    assert result.final_status == "cancelled"


def test_fake_tool_router_progress_events_in_order():
    """Progress stages emitted in the order passed to the constructor."""
    stages: list[ProgressStage] = ["started", "scanning", "aggregating"]
    router = FakeToolRouter(session_id="s1", progress_stages=stages)
    result = _run(router.dispatch(_make_request()))
    progress_evts = [e for e in result.events if e.event_type == "tool_progress_event"]
    emitted_stages = [e.payload_inline["progress_stage"] for e in progress_evts]  # type: ignore[index]
    assert emitted_stages == stages


def test_fake_tool_router_tool_call_id_deterministic():
    """Same tool_name + counter → same tool_call_id (invariant #5)."""
    id1 = _deterministic_id("lookup", 1)
    id2 = _deterministic_id("lookup", 1)
    id3 = _deterministic_id("lookup", 2)
    assert id1 == id2
    assert id1 != id3


def test_fake_tool_router_local_only_raises_not_implemented():
    """local_only privacy mode raises NotImplementedError on dispatch (OQ-11)."""
    router = FakeToolRouter(session_id="s1", privacy_mode="local_only")
    with pytest.raises(NotImplementedError, match="#96"):
        _run(router.dispatch(_make_request()))


def test_fake_tool_router_satisfies_protocol():
    """FakeToolRouter must satisfy isinstance check for ToolRouter Protocol."""
    router = FakeToolRouter()
    assert isinstance(router, ToolRouter)


def test_fake_tool_router_routing_tier_recorded_on_dispatched():
    """tool_call_dispatched carries routing_tier per Anchor 3."""
    router = FakeToolRouter(session_id="s1", progress_stages=[])
    result = _run(router.dispatch(_make_request(routing_hint="smart")))
    dispatched = next(e for e in result.events if e.event_type == "tool_call_dispatched")
    assert dispatched.payload_inline is not None
    assert dispatched.payload_inline.get("routing_tier") == "smart"
