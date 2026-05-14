"""EventLogger — async non-blocking unit test.

Success criterion (ROADMAP Task 2): a synthetic high-rate event stream does
not block on a slow sink.
"""

import asyncio
import time

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


def _make_event(i: int) -> Event:
    return Event(
        event_id=f"evt-{i}",
        session_id="test-session",
        schema_version="0.1",
        seq_no=i,
        event_type="test_event",
        timestamp_mono_ms=int(time.monotonic() * 1000),
        timestamp_wall="",
        source="test",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="default",
    )


@pytest.mark.asyncio
async def test_high_rate_stream_does_not_block_on_slow_sink():
    """Enqueuing 2000 events into a queue maxsize=64 (with a 50ms-per-event sink)
    must complete in well under 1 second — confirming the realtime path never waits
    on sink durability. Dropped events produce log_drop_or_degrade entries instead
    of blocking.
    """
    received: list[str] = []

    async def slow_sink(event: Event) -> None:
        await asyncio.sleep(0.05)  # 50 ms per event — deliberately slow
        received.append(event.event_type)

    logger = EventLogger(slow_sink, maxsize=64)
    await logger.start()

    n = 2000
    t0 = time.monotonic()
    for i in range(n):
        logger.log(_make_event(i))
    elapsed = time.monotonic() - t0

    # Enqueuing 2000 events must take well under 1 second even with a 50ms sink
    assert elapsed < 1.0, f"log() calls blocked: {elapsed:.3f}s for {n} events"

    await logger.stop()

    # The log_drop_or_degrade path is exercised: queue (64) << burst (2000)
    degrade_count = sum(1 for t in received if t == "log_drop_or_degrade")
    assert degrade_count > 0, "expected degrade events when burst >> queue capacity"
