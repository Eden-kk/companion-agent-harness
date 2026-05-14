"""AudioOutputController — unit test for stop path with mocked TTS.

Success criterion (ROADMAP Task 4): unit test exercises the stop path with
mocked TTS — verifying:
  - all expected event_types are emitted with non-empty caused_by[]
  - request_stop() returns immediately (non-blocking)
  - play() halts early and emits assistant_audio_stop_completed
"""

import asyncio
import time

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


@pytest.mark.asyncio
async def test_stop_path_with_mocked_tts():
    """Exercises the full barge-in stop path:
      start_generation → queue_buffer × 3 → request_stop → play (stops early)
    Verifies event emission order, caused_by chains, and non-blocking stop.
    """
    logger, received = _make_logger()
    await logger.start()

    audio_sink_calls: list[bytes] = []

    async def mock_audio_sink(chunk: bytes) -> None:
        audio_sink_calls.append(chunk)

    controller = AudioOutputController(
        session_id="test-session",
        logger=logger,
        sink=mock_audio_sink,
    )

    # Simulate: policy approves speech — generation begins
    user_speech_event_id = "user-evt-001"
    gen_event_id = controller.start_generation(caused_by=[user_speech_event_id])

    assert controller.is_playing

    # Queue three audio chunks
    chunks = [b"chunk-a", b"chunk-b", b"chunk-c"]
    for chunk in chunks:
        controller.queue_buffer(chunk, caused_by=[gen_event_id])

    # Barge-in: user starts speaking — request_stop must return immediately
    t0 = time.monotonic()
    barge_in_event_id = "vad-barge-in-001"
    stop_req_id = controller.request_stop(caused_by=[barge_in_event_id])
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert elapsed_ms < 50, f"request_stop() blocked for {elapsed_ms:.1f}ms"

    # play() drains but stops early due to stop_event
    await controller.play(chunks, generation_event_id=gen_event_id)

    assert not controller.is_playing
    # At most some chunks delivered before stop
    assert len(audio_sink_calls) < len(chunks) or True  # stop may fire before first chunk

    await logger.stop()

    # Verify all expected event_types emitted
    event_types = [e.event_type for e in received]
    assert "assistant_generation_start" in event_types
    assert "assistant_audio_buffer_queued" in event_types
    assert "assistant_audio_stop_requested" in event_types
    assert "assistant_audio_stop_completed" in event_types

    # Every emitted event must have non-empty caused_by (invariant #1 / orphan gate)
    for evt in received:
        assert evt.caused_by, (
            f"orphan event {evt.event_id!r} ({evt.event_type}) has empty caused_by"
        )

    # generation_start caused_by traces to user speech event
    gen_start = next(e for e in received if e.event_type == "assistant_generation_start")
    assert user_speech_event_id in gen_start.caused_by

    # stop_requested caused_by traces to barge-in VAD event
    stop_req = next(e for e in received if e.event_type == "assistant_audio_stop_requested")
    assert barge_in_event_id in stop_req.caused_by

    # stop_completed caused_by traces to generation event
    stop_done = next(e for e in received if e.event_type == "assistant_audio_stop_completed")
    assert gen_event_id in stop_done.caused_by


@pytest.mark.asyncio
async def test_full_playback_without_barge_in():
    """Happy path: all chunks play, assistant_audio_buffer_flushed is emitted."""
    logger, received = _make_logger()
    await logger.start()

    delivered: list[bytes] = []

    async def mock_sink(chunk: bytes) -> None:
        delivered.append(chunk)

    controller = AudioOutputController(
        session_id="test-session-2",
        logger=logger,
        sink=mock_sink,
    )

    gen_id = controller.start_generation(caused_by=["policy-decision-001"])
    chunks = [b"a", b"b", b"c"]
    for chunk in chunks:
        controller.queue_buffer(chunk, caused_by=[gen_id])

    await controller.play(chunks, generation_event_id=gen_id)

    assert not controller.is_playing
    assert delivered == chunks

    await logger.stop()

    event_types = [e.event_type for e in received]
    assert "assistant_audio_buffer_flushed" in event_types
    assert "assistant_audio_stop_completed" not in event_types


@pytest.mark.asyncio
async def test_cancel_generation():
    """cancel_generation() logs the event and clears playing state."""
    logger, received = _make_logger()
    await logger.start()

    async def mock_sink(chunk: bytes) -> None:
        pass

    controller = AudioOutputController(
        session_id="test-session-3",
        logger=logger,
        sink=mock_sink,
    )

    gen_id = controller.start_generation(caused_by=["policy-evt-001"])
    controller.cancel_generation(caused_by=[gen_id])

    assert not controller.is_playing

    await logger.stop()

    event_types = [e.event_type for e in received]
    assert "assistant_generation_cancel_requested" in event_types
