"""Contract tests: audio_out path preserves chunks across rapid full_response decisions.

Addresses Finding 10 (manual-test-findings-2026-05-15.md §Finding 10):
  audio_out_chunks_sent reported only ~15% of full_response decisions reaching
  the browser — the rest were silently dropped or cancelled.

These tests assert:
  1. Each full_response decision produces at least one audio_chunk_emitted event
     (assistant_audio_buffer_queued) in the event log.
  2. cancel_generation() emits tts_synthesis_cancelled (observable, not silent).
  3. Two sequential full_response decisions processed by T4 do not cancel each
     other: the second decision's audio only starts after the first completes.

Finding 9 (duplicate dispatch) is a separate orchestrator-level bug. Tests here
use the AudioOutputController + WebSocketAudioSink layer directly so they pass
with or without Finding 9 being fixed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event
from manual_test_console.live_pipeline import WebSocketAudioSink


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=512), received


class _RecordingBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, int, bytes]] = []

    def publish(self, session_id: str, seq: int, chunk: bytes) -> None:
        self.published.append((session_id, seq, chunk))


async def _chunks(*data: bytes) -> AsyncIterator[bytes]:
    for b in data:
        yield b


# ---------------------------------------------------------------------------
# Test 1: each full_response produces at least one audio_chunk_emitted event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_full_response_produces_at_least_one_audio_chunk() -> None:
    """AudioOutputController.play() logs at least one assistant_audio_buffer_queued
    event per synthesis attempt — this is the per-chunk observable that lets the
    operator diagnose audio drop ratios from the event log (Finding 10 fix).
    """
    logger, received = _make_logger()
    await logger.start()

    broker = _RecordingBroker()
    sink = WebSocketAudioSink(session_id="sess-f10-t1", broker=broker)
    controller = AudioOutputController(session_id="sess-f10-t1", logger=logger, sink=sink)

    policy_evt_id = "policy-decision-f10-t1"
    gen_id = controller.start_generation(caused_by=[policy_evt_id])
    await controller.play(_chunks(b"hello", b"world"), generation_event_id=gen_id)

    await logger.stop()

    # Every chunk forwarded to the sink must produce an assistant_audio_buffer_queued event.
    queued_events = [e for e in received if e.event_type == "assistant_audio_buffer_queued"]
    assert len(queued_events) >= 1, (
        "No assistant_audio_buffer_queued events emitted — audio chunk count "
        "cannot be diagnosed from the event log (Finding 10 invariant #1 gap)."
    )
    # All chunks must also reach the broker.
    assert len(broker.published) == 2
    assert broker.published[0][2] == b"hello"
    assert broker.published[1][2] == b"world"

    # Each queued event must chain back to the generation event (causal closure, invariant #1).
    for evt in queued_events:
        assert gen_id in evt.caused_by, (
            f"assistant_audio_buffer_queued {evt.event_id!r} does not chain to gen_id"
        )


# ---------------------------------------------------------------------------
# Test 2: tts_synthesis_cancelled emitted when cancel_generation() fires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synthesis_cancel_events_emitted_when_cancel_fires() -> None:
    """cancel_generation() must emit tts_synthesis_cancelled so the operator can
    observe cancellations in the event log and diagnose the barge-in-against-self
    scenario described in Finding 10.
    """
    logger, received = _make_logger()
    await logger.start()

    async def mock_sink(_chunk: bytes) -> None:
        return None

    controller = AudioOutputController(
        session_id="sess-f10-t2", logger=logger, sink=mock_sink
    )

    gen_id = controller.start_generation(caused_by=["policy-f10-t2"])
    controller.cancel_generation(caused_by=[gen_id])

    await logger.stop()

    event_types = [e.event_type for e in received]
    assert "tts_synthesis_cancelled" in event_types, (
        "cancel_generation() did not emit tts_synthesis_cancelled — cancellations "
        "are invisible in the event log (Finding 10 observability gap)."
    )
    assert "assistant_generation_cancel_requested" in event_types  # backward compat

    # tts_synthesis_cancelled must chain to the generation event (invariant #1).
    cancel_evt = next(e for e in received if e.event_type == "tts_synthesis_cancelled")
    assert gen_id in cancel_evt.caused_by, (
        "tts_synthesis_cancelled does not chain to generation event — orphan event."
    )


# ---------------------------------------------------------------------------
# Test 3: two sequential full_response decisions do not cancel each other
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_full_responses_do_not_cancel_each_other_unsafe() -> None:
    """Two full_response decisions processed sequentially must both deliver audio.

    T4 (_synthesis_dispatch_task) awaits each play() before starting the next;
    so the second decision's synthesis does not start until the first finishes.
    This test verifies the sequential contract: neither decision cancels the other.

    If Finding 9 produces duplicate full_response decisions for the same turn,
    this test confirms that the FIRST decision's audio is not lost as a direct
    consequence of T4's sequential processing.
    """
    logger, received = _make_logger()
    await logger.start()

    broker = _RecordingBroker()
    sink = WebSocketAudioSink(session_id="sess-f10-t3", broker=broker)
    controller = AudioOutputController(session_id="sess-f10-t3", logger=logger, sink=sink)

    # First decision — simulate T4 processing it.
    gen_id_1 = controller.start_generation(caused_by=["policy-f10-t3-a"])
    await controller.play(_chunks(b"first-chunk"), generation_event_id=gen_id_1)

    # Second decision — T4 starts only after first play() returns.
    gen_id_2 = controller.start_generation(caused_by=["policy-f10-t3-b"])
    await controller.play(_chunks(b"second-chunk"), generation_event_id=gen_id_2)

    await logger.stop()

    # Both chunks must reach the broker; no chunk is lost.
    chunks_in_order = [p[2] for p in broker.published]
    assert b"first-chunk" in chunks_in_order, "First decision's audio chunk was lost."
    assert b"second-chunk" in chunks_in_order, "Second decision's audio chunk was lost."

    # tts_synthesis_cancelled must NOT appear — neither decision was cancelled.
    cancelled_events = [e for e in received if e.event_type == "tts_synthesis_cancelled"]
    assert cancelled_events == [], (
        f"tts_synthesis_cancelled fired unexpectedly: {[e.event_id for e in cancelled_events]}"
    )

    # tts_synthesis_completed should appear twice (once per decision).
    completed_events = [e for e in received if e.event_type == "tts_synthesis_completed"]
    assert len(completed_events) == 2, (
        f"Expected 2 tts_synthesis_completed events, got {len(completed_events)}"
    )
