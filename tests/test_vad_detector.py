"""VADDetector — unit test for TurnSignal emission on a scripted speech/silence sample.

Success criterion (ROADMAP Task 5): detector emits a TurnSignal on a recorded
speech/silence sample without importing torch or any GPU dependency.
"""

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, TurnSignal
from companion_harness.turn_detector_vad import VADDetector, VADModel


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


class _ScriptedVADModel:
    """Fake VADModel that returns probabilities from a scripted sequence."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def __call__(self, frame: bytes) -> float:
        return next(self._probs)


@pytest.mark.asyncio
async def test_detector_emits_turn_signal_on_speech_then_silence():
    """Feed scripted speech frames followed by silence frames.

    Expects exactly one TurnSignal emitted after silence_onset_ms of silence,
    with detector='vad', p_done > 0.5, and non-empty evidence_event_ids.
    """
    logger, received = _make_logger()
    await logger.start()

    # 10 speech frames (p=0.9) then 10 silence frames (p=0.1).
    # frame_duration_ms=32, silence_onset_ms=300 → need ceil(300/32)=10 silence frames.
    probs = [0.9] * 10 + [0.1] * 10
    model = _ScriptedVADModel(probs)
    detector = VADDetector(
        model=model,
        session_id="test-session",
        logger=logger,
        frame_duration_ms=32,
        silence_onset_ms=300,
    )

    audio_frame = b"\x00" * 512
    input_event_id = "audio-input-001"

    signals: list[TurnSignal] = []
    for _ in probs:
        result = detector.process_frame(audio_frame, caused_by=[input_event_id])
        if result is not None:
            signals.append(result)

    await logger.stop()

    assert len(signals) == 1, f"expected 1 TurnSignal, got {len(signals)}"
    sig = signals[0]
    assert sig.detector == "vad"
    assert sig.p_done > 0.5
    assert sig.p_continue < 0.5
    assert sig.p_backchannel == 0.0
    assert sig.confidence > 0.5
    assert len(sig.evidence_event_ids) == 1

    # Every emitted event must have non-empty caused_by (invariant #1)
    for evt in received:
        assert evt.caused_by, (
            f"orphan event {evt.event_id!r} ({evt.event_type}) has empty caused_by"
        )

    # vad_turn_signal event must be logged
    event_types = [e.event_type for e in received]
    assert "vad_turn_signal" in event_types
    assert "vad_frame" in event_types


@pytest.mark.asyncio
async def test_detector_does_not_emit_during_continuous_speech():
    """Continuous speech frames produce no TurnSignal."""
    logger, received = _make_logger()
    await logger.start()

    model = _ScriptedVADModel([0.9] * 20)
    detector = VADDetector(
        model=model,
        session_id="test-session-2",
        logger=logger,
        frame_duration_ms=32,
        silence_onset_ms=300,
    )

    audio_frame = b"\x00" * 512
    signals = [
        detector.process_frame(audio_frame, caused_by=["audio-input-002"])
        for _ in range(20)
    ]

    await logger.stop()

    assert all(s is None for s in signals), "expected no TurnSignal during continuous speech"


def test_no_torch_import():
    """Confirm torch is not imported by the vad_detector module."""
    import inspect
    import companion_harness.turn_detector_vad as vad_mod

    source = inspect.getsource(vad_mod)
    assert "import torch" not in source, "torch must not be imported in turn_detector_vad"
    assert "from torch" not in source, "torch must not be imported in turn_detector_vad"
