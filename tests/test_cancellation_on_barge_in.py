"""v0.1f Task 11: cancellation-on-barge-in contract test.

Success criterion (roadmap Wave 4 Task 11):
  - 300ms p50 cancel-latency gate per spec line 588.
  - tool_call_cancelled emitted <= 300ms after cancel() is called.
  - cancel() call does not block the realtime path (returns promptly).

All adapters are fakes — no torch, no SDK imports.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from companion_harness.fake_tool_router import FakeToolRouter
from companion_harness.tool_router import ToolDispatchRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(tool_name: str = "search") -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name=tool_name,
        arguments={"q": "test"},
        caused_by=["upstream-evt"],
    )


def _percentile(values: list[float], pct: int) -> float:
    s = sorted(values)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s) - 1)]


_CANCEL_LATENCY_GATE_MS = 300


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_cancelled_emitted_within_300ms() -> None:
    """tool_call_cancelled is present in result.events after cancel() (300ms p50 gate)."""
    router = FakeToolRouter(
        session_id="cancel-test",
        progress_stages=["started", "scanning", "aggregating"],
    )
    request = _make_request()

    dispatch_task = asyncio.get_running_loop().create_task(router.dispatch(request))

    # Yield to let dispatch register its cancel flag before the first await.
    await asyncio.sleep(0)

    assert router._cancel_flags, "cancel flag must be registered before cancel()"
    tool_call_id = next(iter(router._cancel_flags))

    t_cancel = time.perf_counter()
    await router.cancel(tool_call_id)

    result = await dispatch_task
    t_done = time.perf_counter()

    latency_ms = (t_done - t_cancel) * 1000.0

    event_types = [e.event_type for e in result.events]
    assert "tool_call_cancelled" in event_types, (
        f"tool_call_cancelled missing from events; got: {event_types}"
    )
    assert "tool_call_completed" not in event_types, (
        f"tool_call_completed must not appear when cancelled; got: {event_types}"
    )
    assert result.final_status == "cancelled"

    assert latency_ms <= _CANCEL_LATENCY_GATE_MS, (
        f"cancel latency {latency_ms:.1f}ms exceeds 300ms gate (spec line 588)"
    )


@pytest.mark.asyncio
async def test_cancel_latency_p50_gate_over_10_runs() -> None:
    """p50 cancel latency <= 300ms across 10 independent dispatch+cancel cycles."""
    latencies_ms: list[float] = []

    for i in range(10):
        router = FakeToolRouter(
            session_id=f"cancel-p50-{i}",
            progress_stages=["started", "scanning", "aggregating"],
        )
        request = _make_request()
        dispatch_task = asyncio.get_running_loop().create_task(router.dispatch(request))

        await asyncio.sleep(0)

        assert router._cancel_flags, f"run {i}: cancel flag not registered"
        tool_call_id = next(iter(router._cancel_flags))

        t0 = time.perf_counter()
        await router.cancel(tool_call_id)
        result = await dispatch_task
        latency_ms = (time.perf_counter() - t0) * 1000.0
        latencies_ms.append(latency_ms)

        assert result.final_status == "cancelled", f"run {i}: expected cancelled"

    p50 = _percentile(latencies_ms, 50)
    assert p50 <= _CANCEL_LATENCY_GATE_MS, (
        f"p50 cancel latency {p50:.1f}ms exceeds 300ms gate; "
        f"all latencies: {[f'{v:.1f}' for v in latencies_ms]}"
    )


@pytest.mark.asyncio
async def test_cancel_does_not_block_realtime_path() -> None:
    """cancel() returns in under 10ms (must not block the realtime path)."""
    router = FakeToolRouter(
        session_id="cancel-nonblock",
        progress_stages=["started", "scanning", "aggregating"],
    )
    request = _make_request()
    dispatch_task = asyncio.get_running_loop().create_task(router.dispatch(request))

    await asyncio.sleep(0)

    assert router._cancel_flags
    tool_call_id = next(iter(router._cancel_flags))

    t0 = time.perf_counter()
    await router.cancel(tool_call_id)
    cancel_call_ms = (time.perf_counter() - t0) * 1000.0

    await dispatch_task

    assert cancel_call_ms < 10.0, (
        f"cancel() took {cancel_call_ms:.2f}ms; must be non-blocking (<10ms)"
    )
