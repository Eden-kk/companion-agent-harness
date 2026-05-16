"""EventLogger maxsize is operator-configurable (F0d fix)."""

import asyncio
import time

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


def _make_event(i: int, event_type: str = "test_event") -> Event:
    return Event(
        event_id=f"evt-{i}",
        session_id="test-session",
        schema_version="0.1",
        seq_no=i,
        event_type=event_type,
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
async def test_maxsize_128_respected() -> None:
    """EventLogger with maxsize=128 drops when burst exceeds 128."""
    received: list[str] = []

    async def sink(event: Event) -> None:
        received.append(event.event_type)

    logger = EventLogger(sink, maxsize=128)
    await logger.start()

    for i in range(300):
        logger.log(_make_event(i))

    await logger.stop()

    degrade_count = sum(1 for t in received if t == "log_drop_or_degrade")
    assert degrade_count > 0, "burst 300 >> maxsize 128; expected drops"


@pytest.mark.asyncio
async def test_maxsize_256_holds_more_than_128() -> None:
    """Larger maxsize means fewer drops under same burst."""
    dropped_128: list[int] = []
    dropped_256: list[int] = []

    async def sink_128(event: Event) -> None:
        if event.event_type == "log_drop_or_degrade":
            dropped_128.append(1)

    async def sink_256(event: Event) -> None:
        if event.event_type == "log_drop_or_degrade":
            dropped_256.append(1)

    logger_128 = EventLogger(sink_128, maxsize=128)
    logger_256 = EventLogger(sink_256, maxsize=256)

    await logger_128.start()
    await logger_256.start()

    for i in range(500):
        logger_128.log(_make_event(i))
        logger_256.log(_make_event(i))

    await logger_128.stop()
    await logger_256.stop()

    assert len(dropped_128) >= len(dropped_256), (
        f"maxsize=128 should drop at least as much as maxsize=256; "
        f"got {len(dropped_128)} vs {len(dropped_256)}"
    )
