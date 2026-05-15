"""test_no_latency_regression — v0.1e Task 17.

Roadmap lines 539-548, spec lines 541-543:
  memory operations do not increase foreground p50 response time.

Paired-measurement pattern: control (no SleepTimeAgent) vs treatment
(SleepTimeAgent subscribed with in-memory stub).  1000 synthetic events, p50
wall-clock per log() call, 5% tolerance (10% for p95).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.sleep_time_agent import SleepTimeAgent


# ---------------------------------------------------------------------------
# Helpers (mirror test_sleep_time_agent.py helpers to avoid cross-file deps)
# ---------------------------------------------------------------------------

async def _noop_sink(event: Event) -> None:
    pass


class _InMemoryStore:
    def __init__(self) -> None:
        self.committed: list[MemoryItem] = []

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        self.committed.append(item)

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        return []

    def forget(self, item_id: str) -> None:
        pass

    def hard_delete(self, item_id: str) -> None:
        pass


def _make_event(event_type: str, event_id: str, seq: int = 1) -> Event:
    evt = Event(
        event_id=event_id,
        session_id="bench-session",
        schema_version="0.1",
        seq_no=seq,
        event_type=event_type,
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
    return evt


def _make_candidate(event_id: str, seq: int = 1) -> Event:
    evt = _make_event("memory_write_candidate", event_id, seq)
    object.__setattr__(evt, "payload_dict", {
        "item_id": f"item-{event_id}",
        "store": "episodic",
        "content": {"summary": "bench event"},
        "source_event_id": "src-bench",
        "privacy_mode": "normal",
        "subject_class": "self",
        "privacy_level": "safe",
        "mutability": "system_revisable",
        "retention_policy_id": "ep_default_30d",
        "sensitivity": "safe",
    })
    return evt


_N = 1000
_WARMUP = 50


async def _measure(use_agent: bool) -> list[float]:
    """Return per-call log() durations for _N events after a warmup pass."""
    logger = EventLogger(_noop_sink, maxsize=_N + _WARMUP + 10)
    if use_agent:
        store = _InMemoryStore()
        agent = SleepTimeAgent({"episodic": store}, logger)
        await agent.start()
    await logger.start()

    # mix: ~half memory_write_candidate, half vad_frame (matches production ratio)
    def _evt(i: int) -> Event:
        if i % 2 == 0:
            return _make_candidate(f"evt-bench-{i}", seq=i)
        return _make_event("vad_frame", f"evt-bench-{i}", seq=i)

    # warmup
    for i in range(_WARMUP):
        logger.log(_evt(i))

    # measurement
    timings: list[float] = []
    for i in range(_N):
        t0 = time.perf_counter()
        logger.log(_evt(i))
        timings.append(time.perf_counter() - t0)

    await logger.stop()
    return timings


def _percentile(values: list[float], pct: int) -> float:
    s = sorted(values)
    idx = int(len(s) * pct / 100)
    return s[min(idx, len(s) - 1)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_paired_measurement_5pct_tolerance() -> None:
    """p50 treatment <= p50 control * 1.05 (or within 5 µs if control < 5 µs)."""
    timings_control = await _measure(use_agent=False)
    timings_treatment = await _measure(use_agent=True)

    p50_control = _percentile(timings_control, 50)
    p50_treatment = _percentile(timings_treatment, 50)

    # noise-floor guard: if control p50 is sub-microsecond, use absolute 5 µs tolerance
    if p50_control < 5e-6:
        limit = p50_control + 5e-6
    else:
        limit = p50_control * 1.05

    assert p50_treatment <= limit, (
        f"p50 latency regression: control={p50_control * 1e6:.2f}µs "
        f"treatment={p50_treatment * 1e6:.2f}µs limit={limit * 1e6:.2f}µs"
    )


@pytest.mark.asyncio
async def test_p95_measurement_10pct_tolerance() -> None:
    """p95 treatment <= p95 control * 1.10 (or within 10 µs if control < 5 µs)."""
    timings_control = await _measure(use_agent=False)
    timings_treatment = await _measure(use_agent=True)

    p95_control = _percentile(timings_control, 95)
    p95_treatment = _percentile(timings_treatment, 95)

    if p95_control < 5e-6:
        limit = p95_control + 10e-6
    else:
        limit = p95_control * 1.10

    assert p95_treatment <= limit, (
        f"p95 latency regression: control={p95_control * 1e6:.2f}µs "
        f"treatment={p95_treatment * 1e6:.2f}µs limit={limit * 1e6:.2f}µs"
    )


@pytest.mark.asyncio
async def test_smoke_no_drops() -> None:
    """_dropped counter stays at 0 during 1000-event pump (no backpressure)."""
    logger = EventLogger(_noop_sink, maxsize=_N + _WARMUP + 10)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    await logger.start()

    for i in range(_N):
        evt = _make_candidate(f"evt-drop-{i}", seq=i) if i % 2 == 0 else _make_event("vad_frame", f"evt-drop-{i}", seq=i)
        logger.log(evt)

    await logger.stop()
    assert logger._dropped == 0, f"unexpected drops: {logger._dropped}"
