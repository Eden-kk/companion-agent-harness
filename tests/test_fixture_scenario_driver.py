"""Tests for FixtureScenarioDriver (Phase A.5 Task A.5-3)."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from companion_harness.evals.scenarios.audio_feeder import DirectAudioInputFeeder
from companion_harness.evals.scenarios.fixture import FixtureScenarioDriver
from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock
from companion_harness.schemas import EvaluationCase, Event


def _make_case(fixtures: list[str] | None = None) -> EvaluationCase:
    return EvaluationCase(
        case_id="test-fixture-case",
        stage=0,
        scenario="fixture_driver_test",
        modalities=["audio"],
        fixture_ref="",
        expected_events=[],
        expected_metrics={},
        consent_class="safe_eval_fixture",
        fixtures=fixtures or [],
    )


def _make_driver(
    q: "asyncio.Queue[tuple[bytes, str]]",
    sink: list[Event],
    start_ns: int = 0,
) -> FixtureScenarioDriver:
    clock = SyntheticClock(start_ns=start_ns)
    feeder = DirectAudioInputFeeder(q)
    return FixtureScenarioDriver(audio_in=q, clock=clock, feeder=feeder, event_sink=sink)


@pytest.mark.asyncio
async def test_run_returns_replay_run_with_completed_status() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    sink: list[Event] = []
    driver = _make_driver(q, sink)
    case = _make_case()
    replay_run = await driver.run(case, object(), object())
    assert replay_run.final_status == "completed"
    assert replay_run.timing_mode == "synthetic_clock"
    assert replay_run.case_id == "test-fixture-case"


@pytest.mark.asyncio
async def test_run_emits_benchmark_case_started_and_completed() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    sink: list[Event] = []
    driver = _make_driver(q, sink)
    await driver.run(_make_case(), object(), object())
    event_types = [e.event_type for e in sink]
    assert "benchmark_case_started" in event_types
    assert "benchmark_case_completed" in event_types


@pytest.mark.asyncio
async def test_run_emits_synthetic_clock_tick() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    sink: list[Event] = []
    driver = _make_driver(q, sink)
    await driver.run(_make_case(), object(), object())
    assert any(e.event_type == "synthetic_clock_tick" for e in sink)


@pytest.mark.asyncio
async def test_run_with_raw_fixture_injects_chunks_onto_queue() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_path = Path(tmpdir) / "audio.raw"
        fixture_path.write_bytes(b"\x00" * 320)  # 320 bytes = one 20ms 16kHz mono frame

        q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
        sink: list[Event] = []
        driver = _make_driver(q, sink)
        case = _make_case(fixtures=[str(fixture_path)])
        await driver.run(case, object(), object())

        assert q.qsize() == 1
        chunk_bytes, event_id = q.get_nowait()
        assert chunk_bytes == b"\x00" * 320
        assert "chunk" in event_id


@pytest.mark.asyncio
async def test_run_emits_fixture_audio_chunk_injected_per_chunk() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        f1 = Path(tmpdir) / "a.raw"
        f2 = Path(tmpdir) / "b.raw"
        f1.write_bytes(b"\x01" * 160)
        f2.write_bytes(b"\x02" * 160)

        q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
        sink: list[Event] = []
        driver = _make_driver(q, sink)
        case = _make_case(fixtures=[str(f1), str(f2)])
        await driver.run(case, object(), object())

        inject_events = [e for e in sink if e.event_type == "fixture_audio_chunk_injected"]
        assert len(inject_events) == 2
        for evt in inject_events:
            assert evt.sensitivity == "sensitive"
            assert evt.retention_policy_id == "raw_media_default_300s"


@pytest.mark.asyncio
async def test_run_all_events_have_nonempty_caused_by_or_are_root() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    sink: list[Event] = []
    driver = _make_driver(q, sink)
    await driver.run(_make_case(), object(), object())
    # benchmark_case_started may have empty caused_by (root event); all others must have one
    non_root = [e for e in sink if e.event_type != "benchmark_case_started"]
    for evt in non_root:
        assert evt.caused_by, f"event {evt.event_type} has empty caused_by"


@pytest.mark.asyncio
async def test_run_timestamps_are_monotonically_nondecreasing() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        f = Path(tmpdir) / "audio.raw"
        f.write_bytes(b"\x00" * 320)

        q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
        sink: list[Event] = []
        driver = _make_driver(q, sink)
        await driver.run(_make_case(fixtures=[str(f)]), object(), object())

        mono_ts = [e.timestamp_mono_ms for e in sink]
        for a, b in zip(mono_ts, mono_ts[1:]):
            assert a <= b, f"timestamp went backward: {a} > {b}"
