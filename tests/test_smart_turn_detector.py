"""SmartTurnDetector — implementation tests (v0.1b Task 3).

Success criterion: a scripted speech/pause/continuation probability trace fed
through the detector (with a fake SmartTurnModel) produces a TurnSignal with
p_continue > p_done during the pause window.

Non-vacuous: the assertions are tight enough that a broken detector (e.g. one
that never invokes the model, or invokes it on every frame, or emits when
p_continue > p_done) will cause at least one assertion to fail.
"""

import struct

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, TurnSignal
from companion_harness.turn_detector_smart import SmartTurnDetector, SmartTurnModel


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


def _speech_frame(n_samples: int = 256) -> bytes:
    """PCM-16 frame with amplitude well above silence_rms_threshold (default 500)."""
    sample = struct.pack("<h", 2000)
    return sample * n_samples


def _silence_frame(n_samples: int = 256) -> bytes:
    """PCM-16 frame with zero amplitude — RMS = 0."""
    return b"\x00" * (n_samples * 2)


class _ScriptedSmartTurnModel:
    """Fake SmartTurnModel returning scripted (p_done, p_continue) pairs."""

    def __init__(self, pairs: list[tuple[float, float]]) -> None:
        self._pairs = iter(pairs)
        self.call_count = 0

    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        self.call_count += 1
        return next(self._pairs)


@pytest.mark.asyncio
async def test_thinking_pause_emits_p_continue_greater_than_p_done():
    """Scripted trace: speech → silence pause → model says p_continue > p_done.

    Feeds 10 speech frames, then enough silence frames to cross silence_onset_ms.
    The fake model returns (p_done=0.2, p_continue=0.8) at the silence candidate.
    Expects exactly one TurnSignal with p_continue > p_done (thinking-pause window).
    """
    logger, received = _make_logger()
    await logger.start()

    # Model returns p_continue > p_done (user is mid-thought, not done)
    model = _ScriptedSmartTurnModel([(0.2, 0.8)])
    detector = SmartTurnDetector(
        model=model,
        session_id="test-pause",
        logger=logger,
        silence_onset_ms=300,
        frame_duration_ms=32,
    )

    # 10 speech frames then 10 silence frames
    # 10 silence frames: 10 × 32 ms = 320 ms >= silence_onset_ms=300 ms -> candidate fires
    signals: list[TurnSignal] = []
    for _ in range(10):
        result = detector.process_frame(_speech_frame(), caused_by=["audio-001"])
        assert result is None  # no signal during speech

    for _ in range(10):
        result = detector.process_frame(_silence_frame(), caused_by=["audio-001"])
        if result is not None:
            signals.append(result)

    await logger.stop()

    # Exactly one signal emitted
    assert len(signals) == 1, f"expected 1 TurnSignal, got {len(signals)}"
    sig = signals[0]
    assert sig.detector == "smart_turn"
    assert sig.p_continue > sig.p_done, (
        f"expected p_continue ({sig.p_continue}) > p_done ({sig.p_done}) in pause window"
    )
    assert sig.p_backchannel == 0.0
    assert len(sig.evidence_event_ids) == 1

    # Model invoked exactly once (silence-candidate-only, not every frame)
    assert model.call_count == 1, (
        f"model should be invoked once at silence candidate, got {model.call_count}"
    )

    # Logged events: invocation event logged, no spurious events on speech frames
    event_types = [e.event_type for e in received]
    assert "smart_turn_invocation" in event_types
    invocation_count = event_types.count("smart_turn_invocation")
    assert invocation_count == 1, (
        f"expected 1 smart_turn_invocation event, got {invocation_count} "
        "(watch-item 17: logging cadence must follow invocation cadence)"
    )

    # All events have non-empty caused_by (invariant #1)
    for evt in received:
        assert evt.caused_by, f"orphan event {evt.event_id!r} has empty caused_by"


@pytest.mark.asyncio
async def test_no_signal_during_continuous_speech():
    """Continuous speech frames produce no TurnSignal and no model invocations."""
    logger, received = _make_logger()
    await logger.start()

    model = _ScriptedSmartTurnModel([])  # should never be called
    detector = SmartTurnDetector(
        model=model,
        session_id="test-speech-only",
        logger=logger,
        frame_duration_ms=32,
        silence_onset_ms=300,
    )

    signals = [
        detector.process_frame(_speech_frame(), caused_by=["audio-002"])
        for _ in range(20)
    ]
    await logger.stop()

    assert all(s is None for s in signals)
    assert model.call_count == 0, "model must not be invoked during continuous speech"
    assert len(received) == 0, "no events should be logged during continuous speech"


@pytest.mark.asyncio
async def test_end_of_turn_emits_signal_and_resets_buffer():
    """When p_done > threshold, TurnSignal is emitted and buffer is reset."""
    logger, received = _make_logger()
    await logger.start()

    # Two separate speech/silence cycles; model returns high p_done on first invocation
    model = _ScriptedSmartTurnModel([(0.9, 0.1), (0.2, 0.8)])
    detector = SmartTurnDetector(
        model=model,
        session_id="test-eot",
        logger=logger,
        silence_onset_ms=300,
        frame_duration_ms=32,
    )

    signals: list[TurnSignal] = []
    # First cycle: speech then silence → p_done=0.9 → signal emitted, buffer reset
    for _ in range(5):
        detector.process_frame(_speech_frame(), caused_by=["audio-003"])
    for _ in range(10):
        result = detector.process_frame(_silence_frame(), caused_by=["audio-003"])
        if result is not None:
            signals.append(result)

    # Second cycle: more speech then silence → p_continue=0.8 → no signal
    for _ in range(5):
        detector.process_frame(_speech_frame(), caused_by=["audio-003"])
    for _ in range(10):
        result = detector.process_frame(_silence_frame(), caused_by=["audio-003"])
        if result is not None:
            signals.append(result)

    await logger.stop()

    assert len(signals) == 2, f"expected 2 TurnSignals (one per silence candidate), got {len(signals)}"
    # First cycle: p_done > p_continue → end-of-turn
    assert signals[0].p_done > signals[0].p_continue
    assert signals[0].detector == "smart_turn"
    # Second cycle: p_continue > p_done → thinking pause
    assert signals[1].p_continue > signals[1].p_done

    # Two invocations total (one per silence-candidate moment)
    assert model.call_count == 2, f"expected 2 model invocations, got {model.call_count}"

    event_types = [e.event_type for e in received]
    invocation_count = event_types.count("smart_turn_invocation")
    assert invocation_count == 2
    signal_count = event_types.count("smart_turn_signal")
    assert signal_count == 2


def test_silence_rms_threshold_default_filters_ambient():
    """Regression: default threshold must reject typical ambient noise (RMS ~150-300).

    Prevents regression to threshold=100 which caused phantom EOU triggers
    on HVAC hum, breath, and keyboard rustle (observed 2026-05-17).
    """
    from companion_harness.turn_detector_smart import _SILENCE_RMS_THRESHOLD

    assert _SILENCE_RMS_THRESHOLD >= 400, (
        f"silence_rms_threshold too low ({_SILENCE_RMS_THRESHOLD}); "
        "ambient noise will trigger phantom EOU"
    )
