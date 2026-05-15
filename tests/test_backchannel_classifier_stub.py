"""BackchannelClassifier — unit tests (v0.1b Tasks 8 and 9).

Task 8 success criterion: isinstance(fake, BackchannelModel) is True;
BackchannelClassifier can be instantiated with a fake model — no model SDK imported.

Task 9 success criterion: a scripted "mm-hmm during assistant speech" trace fed
through the classifier emits a high-p_backchannel TurnSignal with independent
p_done / p_continue values (not arithmetic derivatives of p_backchannel).
"""

import pytest

from companion_harness.backchannel_classifier import (
    BackchannelClassifier,
    BackchannelModel,
    _P_CONTINUE,
    _P_DONE,
)
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


class _FakeBackchannelModel:
    """Scripted model: returns the next probability from the provided sequence."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def __call__(self, frame: bytes) -> float:
        return next(self._probs)


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


def test_fake_satisfies_protocol():
    assert isinstance(_FakeBackchannelModel([0.9]), BackchannelModel)


def test_backchannel_classifier_construction():
    logger, _ = _make_logger()
    classifier = BackchannelClassifier(
        model=_FakeBackchannelModel([0.9]),
        session_id="test-session",
        logger=logger,
    )
    assert isinstance(classifier, BackchannelClassifier)


@pytest.mark.asyncio
async def test_mmmhmm_during_assistant_speech_emits_high_p_backchannel():
    """Task 9 success criterion: scripted mm-hmm trace → high-p_backchannel TurnSignal.

    The scripted trace represents a user emitting a backchannel vocalization
    (p_backchannel=0.92) during assistant speech, followed by a noise-floor
    silence frame (p_backchannel=0.05). The vocalization frame produces a
    high-p_backchannel TurnSignal; the silence frame is below emit_threshold
    (default 0.3) and is suppressed (no event, no TurnSignal) to keep the
    EventLogger queue from being flooded at ~31 Hz during silence (invariant #10).
    """
    logger, received = _make_logger()
    await logger.start()

    # Scripted: frame 0 = vocalization ("mm-hmm"), frame 1 = inter-vocalization silence
    model = _FakeBackchannelModel([0.92, 0.05])
    classifier = BackchannelClassifier(
        model=model,
        session_id="test-task9",
        logger=logger,
    )

    frame_bytes = b"\x00" * 64  # content irrelevant; model is scripted
    cause = ["upstream-event-001"]

    signal_0 = classifier.process_frame(frame_bytes, caused_by=cause)
    signal_1 = classifier.process_frame(frame_bytes, caused_by=cause)

    await logger.stop()

    # Task 9 primary assertion: vocalization frame produces high p_backchannel
    assert signal_0 is not None
    assert signal_0.p_backchannel > 0.8, (
        f"expected p_backchannel > 0.8 for mm-hmm frame, got {signal_0.p_backchannel}"
    )

    # WI-18: p_done and p_continue are the module-level constants, not derived from p_backchannel
    assert signal_0.p_done == _P_DONE
    assert signal_0.p_continue == _P_CONTINUE
    # Verify independence: p_done != p_backchannel and p_continue != 1.0 - p_backchannel
    assert signal_0.p_done != signal_0.p_backchannel
    assert signal_0.p_continue != pytest.approx(1.0 - signal_0.p_backchannel)

    # Silence frame: below emit_threshold (0.3) → suppressed (no event, no TurnSignal).
    assert signal_1 is None

    # Invariant #1: only the vocalization frame is logged; the noise-floor silence
    # frame is not an event-worthy backchannel observation.
    assert len(received) == 1
    assert received[0].event_type == "backchannel_classification"
    assert received[0].caused_by == cause
