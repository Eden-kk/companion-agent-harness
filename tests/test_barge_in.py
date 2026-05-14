"""Stage 1 — assistant must stop within 200ms (VAD-detected) on user interruption.

See docs/architecture-v0.1.md §Part 6 Stage 1, §Part 8 v0.1a acceptance gates:
  vad_detected_user_speech_to_stop_ms_p95   < 200 ms (system-internal, strict)
  physical_user_speech_onset_to_stop_ms_p95 < 350 ms (product-felt, v0.1a loose)
Fixture: barge_in_001 in §Part 6c.
"""

import asyncio
import statistics
import time

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.schemas import Event


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=1024), received


@pytest.mark.asyncio
async def test_barge_in():
    """Fixture barge_in_001: assistant speaking, VAD detects user speech onset.

    Measures vad_detected_user_speech_to_stop_ms over N trials.
    Gate: p95 < 200 ms (docs/architecture-v0.1.md §Part 8).

    The fixture defines 3 audio chunks with a sink that takes ~10ms per chunk,
    simulating real-time playback. barge-in fires after chunk-1 is queued.
    Stop latency is measured from request_stop() call to play() task completion.
    """
    fixture = load_fixture("barge_in_001")
    assert fixture["case_id"] == "barge_in_001"
    assert fixture["expected_metrics"]["assistant_stop_latency_ms_p95"] == "<200"

    latencies_ms: list[float] = []
    n_trials = 30

    for trial in range(n_trials):
        logger, received = _make_logger()
        await logger.start()

        sink_delays: list[float] = []

        async def audio_sink(chunk: bytes, _delays: list[float] = sink_delays) -> None:
            # Simulate ~10ms per chunk to represent real-time audio delivery.
            # This ensures play() is genuinely in-flight when stop fires.
            await asyncio.sleep(0.01)
            _delays.append(0.01)

        controller = AudioOutputController(
            session_id=f"barge-in-trial-{trial}",
            logger=logger,
            sink=audio_sink,
        )

        # Three audio chunks (fixture signal_trace has 3 buffer entries)
        chunks = [b"chunk-a", b"chunk-b", b"chunk-c"]

        # Assistant starts speaking
        gen_event_id = controller.start_generation(caused_by=["policy-decision-001"])
        assert controller.is_playing

        for chunk in chunks:
            controller.queue_buffer(chunk, caused_by=[gen_event_id])

        # Launch play() as a background task — it will block on the sink calls
        play_task = asyncio.create_task(
            controller.play(chunks, generation_event_id=gen_event_id)
        )

        # Give play() time to enter its first sink await (chunk-a delivery in-flight)
        await asyncio.sleep(0.005)

        # VAD detects user speech onset — measure from here to play() task completion
        vad_event_id = "vad-onset-001"
        t_vad_detected = time.monotonic()
        controller.request_stop(caused_by=[vad_event_id])

        # Wait for play() to honor the stop
        await play_task
        t_stop_completed = time.monotonic()

        assert not controller.is_playing
        latency_ms = (t_stop_completed - t_vad_detected) * 1000
        latencies_ms.append(latency_ms)

        await logger.stop()

        # Verify stop event chain for this trial
        event_types = [e.event_type for e in received]
        assert "assistant_generation_start" in event_types
        assert "assistant_audio_stop_requested" in event_types
        assert "assistant_audio_stop_completed" in event_types

        # stop_requested must be caused by the VAD event
        stop_req = next(e for e in received if e.event_type == "assistant_audio_stop_requested")
        assert vad_event_id in stop_req.caused_by, (
            f"trial {trial}: stop_requested not caused by VAD event"
        )

        # No orphan events (invariant #1)
        for evt in received:
            assert evt.caused_by, (
                f"trial {trial}: orphan event {evt.event_id!r} ({evt.event_type})"
            )

    # Gate: vad_detected_user_speech_to_stop_ms_p95 < 200 ms
    latencies_ms.sort()
    p95_idx = int(0.95 * len(latencies_ms))
    p95_ms = latencies_ms[p95_idx]
    p50_ms = statistics.median(latencies_ms)

    assert p95_ms < 200, (
        f"vad_detected_user_speech_to_stop_ms_p95 = {p95_ms:.1f}ms >= 200ms gate\n"
        f"  p50={p50_ms:.1f}ms  max={max(latencies_ms):.1f}ms  n={n_trials}"
    )
