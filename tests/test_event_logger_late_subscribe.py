"""EventLogger.late_subscribe() — contract tests (WATCH-#24).

Verifies that late_subscribe() allows subscription after logger.start(),
does not replay historical events, fans out to multiple subscribers, and
is safe when _drain is iterating _subscribers concurrently.
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
async def test_late_subscribe_receives_post_subscription_events() -> None:
    """late_subscribe() after start() receives events logged after subscription."""
    received: list[str] = []

    async def primary(event: Event) -> None:
        pass

    async def sub(event: Event) -> None:
        received.append(event.event_id)

    logger = EventLogger(primary)
    await logger.start()

    # log one event before subscribing
    logger.log(_make_event(0))
    await asyncio.sleep(0.05)

    logger.late_subscribe(sub)

    logger.log(_make_event(1))
    logger.log(_make_event(2))

    await logger.stop()

    assert "evt-1" in received
    assert "evt-2" in received


@pytest.mark.asyncio
async def test_late_subscribe_does_not_replay_historical_events() -> None:
    """Events logged before late_subscribe() are not replayed to the new subscriber."""
    received: list[str] = []

    async def primary(event: Event) -> None:
        pass

    async def sub(event: Event) -> None:
        received.append(event.event_id)

    logger = EventLogger(primary)
    await logger.start()

    logger.log(_make_event(0))
    await asyncio.sleep(0.05)  # drain evt-0 before subscribing

    logger.late_subscribe(sub)

    await logger.stop()

    assert "evt-0" not in received


@pytest.mark.asyncio
async def test_multiple_late_subscribers_all_receive_events() -> None:
    """Multiple late_subscribe() calls each receive every post-subscription event."""
    recv_a: list[str] = []
    recv_b: list[str] = []

    async def primary(event: Event) -> None:
        pass

    async def sub_a(event: Event) -> None:
        recv_a.append(event.event_id)

    async def sub_b(event: Event) -> None:
        recv_b.append(event.event_id)

    logger = EventLogger(primary)
    await logger.start()

    logger.late_subscribe(sub_a)
    logger.late_subscribe(sub_b)

    for i in range(3):
        logger.log(_make_event(i))

    await logger.stop()

    assert recv_a == ["evt-0", "evt-1", "evt-2"]
    assert recv_b == ["evt-0", "evt-1", "evt-2"]


@pytest.mark.asyncio
async def test_late_subscribe_is_safe_under_drain() -> None:
    """Appending via late_subscribe() while _drain is processing is safe.

    _drain iterates _subscribers without snapshotting, so a concurrent append
    during iteration must not raise. This test calls late_subscribe() while
    events are actively being drained to exercise that path.
    """
    received: list[str] = []
    subscribe_done = asyncio.Event()

    async def slow_primary(event: Event) -> None:
        await asyncio.sleep(0.01)

    async def sub(event: Event) -> None:
        received.append(event.event_id)

    logger = EventLogger(slow_primary, maxsize=128)
    await logger.start()

    # Flood the queue so _drain is busy
    for i in range(10):
        logger.log(_make_event(i))

    # late_subscribe while drain is mid-loop
    logger.late_subscribe(sub)
    subscribe_done.set()

    logger.log(_make_event(99))

    await logger.stop()

    # evt-99 was logged after late_subscribe — must be received
    assert "evt-99" in received
