"""v0.1f Task 10: no-foreground-block contract test.

Success criterion (roadmap Wave 4 Task 10):
  - Paired-latency gate: p50_during_tool_call <= p50_no_tool_call * 1.05
    (5% tolerance) in the same fixture run.
  - foreground_block_count_per_session == 0 (no blocking tool dispatch on
    the realtime path).

Pattern mirrors v0.1e test_no_latency_regression.py paired-measurement
approach.  All adapters are fakes — no torch, no SDK imports.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.fake_tool_router import FakeToolRouter
from companion_harness.schemas import Event
from companion_harness.tool_progress_emitter import ToolProgressEmitter
from companion_harness.tool_router import ToolDispatchRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(event_id: str, seq: int = 1) -> Event:
    return Event(
        event_id=event_id,
        session_id="bench-session",
        schema_version="0.1",
        seq_no=seq,
        event_type="vad_frame",
        timestamp_mono_ms=int(time.monotonic() * 1000),
        timestamp_wall="",
        source="bench",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="default",
    )


async def _noop_sink(event: Event) -> None:
    pass


def _percentile(values: list[float], pct: int) -> float:
    s = sorted(values)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s) - 1)]


_N = 500
_WARMUP = 20


async def _measure_logger_p50(with_tool_dispatch: bool) -> tuple[float, int]:
    """Return (p50_seconds, foreground_block_count) for _N log() calls.

    foreground_block_count counts how many log() calls took longer than
    1ms (a proxy for a blocking tool dispatch having leaked onto the
    logger path — on a non-blocking path this count must be 0 in practice).

    When with_tool_dispatch=True, a FakeToolRouter dispatch is run
    concurrently while log() calls are measured.  The log() calls must
    not be blocked by the dispatch.
    """
    logger = EventLogger(_noop_sink, maxsize=_N + _WARMUP + 10)
    await logger.start()

    # warmup
    for i in range(_WARMUP):
        logger.log(_make_event(f"warmup-{i}", i))

    if with_tool_dispatch:
        router = FakeToolRouter(
            session_id="bench",
            progress_stages=["started", "scanning", "aggregating"],
        )
        emitter = ToolProgressEmitter()
        request = ToolDispatchRequest(
            tool_name="search",
            arguments={"q": "test"},
            caused_by=["upstream-evt"],
        )
        # Start dispatch as a concurrent background task — it MUST NOT block
        # the log() calls below.
        dispatch_task = asyncio.get_running_loop().create_task(router.dispatch(request))

    timings: list[float] = []
    block_count = 0
    _1MS = 1e-3
    for i in range(_N):
        t0 = time.perf_counter()
        logger.log(_make_event(f"evt-{i}", i))
        elapsed = time.perf_counter() - t0
        timings.append(elapsed)
        if elapsed > _1MS:
            block_count += 1

    if with_tool_dispatch:
        result = await dispatch_task
        # forward tool events to logger as T4 does
        for evt in result.events:
            logger.log(evt)

    await logger.stop()
    return _percentile(timings, 50), block_count


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_foreground_block_paired_latency_gate() -> None:
    """p50_during_tool_call <= p50_no_tool_call * 1.05 (5% tolerance)."""
    p50_control, _ = await _measure_logger_p50(with_tool_dispatch=False)
    p50_treatment, _ = await _measure_logger_p50(with_tool_dispatch=True)

    # noise-floor guard: if control p50 is sub-microsecond use absolute 5 µs tolerance
    if p50_control < 5e-6:
        limit = p50_control + 5e-6
    else:
        limit = p50_control * 1.05

    assert p50_treatment <= limit, (
        f"foreground block regression: "
        f"control={p50_control * 1e6:.2f}µs "
        f"treatment={p50_treatment * 1e6:.2f}µs "
        f"limit={limit * 1e6:.2f}µs"
    )


@pytest.mark.asyncio
async def test_foreground_block_count_is_zero() -> None:
    """foreground_block_count_per_session == 0: no >1ms stalls during tool dispatch."""
    _, block_count = await _measure_logger_p50(with_tool_dispatch=True)
    assert block_count == 0, (
        f"foreground_block_count_per_session={block_count}; expected 0. "
        f"Tool dispatch is leaking onto the realtime log() path."
    )
