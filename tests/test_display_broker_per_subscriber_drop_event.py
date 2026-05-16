"""DisplayBroker emits display_subscriber_drop audit event on QueueFull.

Contract tests:
- A full queue triggers a display_subscriber_drop event via the logger.
- drop events for display_subscriber_drop are suppressed (no feedback loop).
- Throttle: at most 1 emission per (subscriber_id, dropped_event_type) per 60s.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from companion_harness.schemas import Event


def _make_event(i: int, event_type: str = "vad_frame") -> Event:
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


class _FakeLogger:
    def __init__(self) -> None:
        self.logged: list[Event] = []

    def log(self, event: Event) -> None:
        self.logged.append(event)


@pytest.mark.asyncio
async def test_drop_emits_display_subscriber_drop() -> None:
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    # Minimum clamped queue is 64; send 80 events to guarantee overflow.
    broker = DisplayBroker(queue_depth=64)
    fake_logger = _FakeLogger()
    broker.set_logger(fake_logger)
    broker.add()

    for i in range(80):
        await broker.on_event(_make_event(i, "policy_decision"))  # high-signal, always forwarded

    drop_events = [e for e in fake_logger.logged if e.event_type == "display_subscriber_drop"]
    assert len(drop_events) >= 1, "expected at least one display_subscriber_drop event"

    drop = drop_events[0]
    assert drop.source == "display_broker"
    assert drop.sensitivity == "safe"
    assert drop.subject_class == "self"
    assert drop.payload_inline is not None
    assert "subscriber_id" in drop.payload_inline
    assert "subscriber_drop_count" in drop.payload_inline
    assert drop.payload_inline["dropped_event_type"] == "policy_decision"
    assert len(drop.caused_by) == 1


@pytest.mark.asyncio
async def test_drop_event_itself_not_re_emitted() -> None:
    """display_subscriber_drop events are not emitted when the dropped event is itself a drop."""
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker(queue_depth=64)
    fake_logger = _FakeLogger()
    broker.set_logger(fake_logger)
    broker.add()

    # Flood with regular events to get a drop logged.
    for i in range(80):
        await broker.on_event(_make_event(i, "policy_decision"))

    count_before = len([e for e in fake_logger.logged if e.event_type == "display_subscriber_drop"])

    # Now send display_subscriber_drop events into a full queue — must NOT emit more drops.
    drop_evt = Event(
        event_id="drop-99",
        session_id="",
        schema_version="0.1",
        seq_no=99,
        event_type="display_subscriber_drop",
        timestamp_mono_ms=int(time.monotonic() * 1000),
        timestamp_wall="",
        source="display_broker",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )
    for _ in range(10):
        await broker.on_event(drop_evt)

    count_after = len([e for e in fake_logger.logged if e.event_type == "display_subscriber_drop"])
    assert count_after == count_before, (
        f"display_subscriber_drop count grew {count_before} → {count_after}: feedback loop detected"
    )


@pytest.mark.asyncio
async def test_throttle_one_drop_event_per_60s_window() -> None:
    """80 overflowing policy_decision events → exactly 1 display_subscriber_drop within window."""
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker(queue_depth=64)
    fake_logger = _FakeLogger()
    broker.set_logger(fake_logger)
    broker.add()

    for i in range(80):
        await broker.on_event(_make_event(i, "policy_decision"))

    policy_drops = [
        e for e in fake_logger.logged
        if e.event_type == "display_subscriber_drop"
        and (e.payload_inline or {}).get("dropped_event_type") == "policy_decision"
    ]
    assert len(policy_drops) == 1, (
        f"expected exactly 1 throttled drop event, got {len(policy_drops)}"
    )
