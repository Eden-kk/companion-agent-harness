"""DisplayBroker samples high-rate frame events before display fanout (F0d fix).

Audit invariant #1 is unaffected — the EventLogger ring records every event.
Sampling only applies to the display WS fanout path.
"""

import asyncio
import time

import pytest

from companion_harness.schemas import Event


def _make_event(i: int, event_type: str) -> Event:
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
async def test_sampling_rate_5_forwards_expected_counts() -> None:
    """100 raw_audio_chunk + 100 vad_frame + 10 policy_decision → 20+20+10 = 50."""
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker(sampling_rate=5)
    q = broker.add()

    # Push 100 raw_audio_chunk events (sampling_rate=5 → 20 forwarded)
    for i in range(100):
        await broker.on_event(_make_event(i, "raw_audio_chunk"))

    # Push 100 vad_frame events (sampling_rate=5 → 20 forwarded)
    for i in range(100):
        await broker.on_event(_make_event(i, "vad_frame"))

    # Push 10 policy_decision events (always forwarded)
    for i in range(10):
        await broker.on_event(_make_event(i, "policy_decision"))

    received = []
    while not q.empty():
        received.append(q.get_nowait())

    assert len(received) == 50, f"expected 50, got {len(received)}"

    types = [m["event"]["event_type"] for m in received]
    audio_count = types.count("raw_audio_chunk")
    vad_count = types.count("vad_frame")
    policy_count = types.count("policy_decision")

    assert audio_count == 20, f"expected 20 raw_audio_chunk, got {audio_count}"
    assert vad_count == 20, f"expected 20 vad_frame, got {vad_count}"
    assert policy_count == 10, f"expected 10 policy_decision, got {policy_count}"


@pytest.mark.asyncio
async def test_sampling_rate_1_no_sampling() -> None:
    """sampling_rate=1 disables sampling — all events forwarded."""
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker(sampling_rate=1)
    q = broker.add()

    for i in range(50):
        await broker.on_event(_make_event(i, "raw_audio_chunk"))
    for i in range(50):
        await broker.on_event(_make_event(i, "vad_frame"))

    received = []
    while not q.empty():
        received.append(q.get_nowait())

    assert len(received) == 100, f"expected 100 with no sampling, got {len(received)}"


@pytest.mark.asyncio
async def test_sampling_summary_tracks_counts() -> None:
    """sampling_summary() returns correct forwarded/sampled_out tallies."""
    from manual_test_console.server import DisplayBroker  # noqa: WPS433

    broker = DisplayBroker(sampling_rate=5)
    broker.add()

    for i in range(100):
        await broker.on_event(_make_event(i, "raw_audio_chunk"))

    summary = broker.sampling_summary()
    assert summary["forwarded"].get("raw_audio_chunk", 0) == 20
    assert summary["sampled_out"].get("raw_audio_chunk", 0) == 80

    # Reset confirmed: second call should show zeros
    summary2 = broker.sampling_summary()
    assert summary2["forwarded"].get("raw_audio_chunk", 0) == 0
    assert summary2["sampled_out"].get("raw_audio_chunk", 0) == 0
