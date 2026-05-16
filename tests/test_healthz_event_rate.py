"""T6: _EventRateCounter sliding-window event-rate accuracy."""

from __future__ import annotations

import pytest

from manual_test_console.server import _EventRateCounter
from companion_harness.schemas import Event


def _make_event(ts_ms: int) -> Event:
    return Event(
        event_id=f"e-{ts_ms}",
        session_id="test",
        schema_version="0.1",
        seq_no=1,
        event_type="raw_audio",
        timestamp_mono_ms=ts_ms,
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
async def test_rate_zero_initially():
    counter = _EventRateCounter(window_seconds=60)
    assert counter.rate_per_second() == 0.0
    assert counter.total_count() == 0


@pytest.mark.asyncio
async def test_rate_counts_events_in_window():
    counter = _EventRateCounter(window_seconds=60)
    for i in range(60):
        await counter.on_event(_make_event(i * 1000))
    assert counter.total_count() == 60
    assert counter.rate_per_second() == pytest.approx(1.0, abs=0.1)


@pytest.mark.asyncio
async def test_old_events_trimmed_from_window():
    counter = _EventRateCounter(window_seconds=60)
    now_ms = 200_000
    old_events = [_make_event(now_ms - 120_000 + i * 1000) for i in range(10)]
    recent_events = [_make_event(now_ms - 30_000 + i * 5000) for i in range(5)]
    for e in old_events + recent_events:
        await counter.on_event(e)
    # Last event ts = 190_000; cutoff = 130_000
    # old events (80000-89000) all trimmed; recent (170000-190000) kept
    assert counter.total_count() == 15
    assert len(counter._timestamps) == 5


@pytest.mark.asyncio
async def test_total_count_is_monotonic():
    counter = _EventRateCounter(window_seconds=60)
    for i in range(10):
        await counter.on_event(_make_event(i * 100_000))
    assert counter.total_count() == 10


@pytest.mark.asyncio
async def test_rate_per_second_accuracy_within_one():
    counter = _EventRateCounter(window_seconds=60)
    # 120 events over a 59-second span → ~2 events/sec
    for i in range(120):
        await counter.on_event(_make_event(i * 500))
    rate = counter.rate_per_second()
    assert abs(rate - 2.0) < 1.0, f"rate {rate} not within 1 of expected 2.0"
