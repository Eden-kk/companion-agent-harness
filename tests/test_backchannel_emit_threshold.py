"""BackchannelClassifier emit-threshold gate (Finding 3 fix).

Verifies that `BackchannelClassifier.process_frame()` suppresses event emission
and TurnSignal return when `p_backchannel < emit_threshold` (default 0.3), so
the EventLogger queue is not flooded at ~31 Hz during silence (invariant #10).

Success criterion:
  - 100 sub-threshold frames (p_backchannel=0.05) produce 0 TurnSignals and
    0 backchannel_classification events.
  - 100 above-threshold frames (p_backchannel=0.5) produce 100 TurnSignals and
    100 backchannel_classification events.
"""

import pytest

from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


class _ConstantBackchannelModel:
    def __init__(self, prob: float) -> None:
        self._prob = prob

    def __call__(self, frame: bytes) -> float:
        return self._prob


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


@pytest.mark.asyncio
async def test_sub_threshold_frames_are_suppressed():
    """p_backchannel=0.05 (below default emit_threshold=0.3) → 0 events, 0 signals."""
    logger, received = _make_logger()
    await logger.start()

    classifier = BackchannelClassifier(
        model=_ConstantBackchannelModel(0.05),
        session_id="test-bc-suppress",
        logger=logger,
    )

    signals_returned = 0
    for i in range(100):
        sig = classifier.process_frame(b"\x00" * 64, caused_by=[f"chunk-{i}"])
        if sig is not None:
            signals_returned += 1

    await logger.stop()

    assert signals_returned == 0, (
        f"expected 0 TurnSignals at p_backchannel=0.05, got {signals_returned}"
    )
    bc_events = [e for e in received if e.event_type == "backchannel_classification"]
    assert len(bc_events) == 0, (
        f"expected 0 backchannel_classification events at p_backchannel=0.05, "
        f"got {len(bc_events)}"
    )


@pytest.mark.asyncio
async def test_above_threshold_frames_emit_every_time():
    """p_backchannel=0.5 (above default emit_threshold=0.3) → 100 events, 100 signals."""
    logger, received = _make_logger()
    await logger.start()

    classifier = BackchannelClassifier(
        model=_ConstantBackchannelModel(0.5),
        session_id="test-bc-emit",
        logger=logger,
    )

    signals_returned = 0
    for i in range(100):
        sig = classifier.process_frame(b"\x00" * 64, caused_by=[f"chunk-{i}"])
        if sig is not None:
            signals_returned += 1

    await logger.stop()

    assert signals_returned == 100, (
        f"expected 100 TurnSignals at p_backchannel=0.5, got {signals_returned}"
    )
    bc_events = [e for e in received if e.event_type == "backchannel_classification"]
    assert len(bc_events) == 100, (
        f"expected 100 backchannel_classification events at p_backchannel=0.5, "
        f"got {len(bc_events)}"
    )


@pytest.mark.asyncio
async def test_custom_emit_threshold_is_respected():
    """emit_threshold kwarg overrides default; frame at 0.4 is suppressed when threshold=0.5."""
    logger, received = _make_logger()
    await logger.start()

    classifier = BackchannelClassifier(
        model=_ConstantBackchannelModel(0.4),
        session_id="test-bc-custom-threshold",
        logger=logger,
        emit_threshold=0.5,
    )

    sig = classifier.process_frame(b"\x00" * 64, caused_by=["chunk-0"])
    await logger.stop()

    assert sig is None
    bc_events = [e for e in received if e.event_type == "backchannel_classification"]
    assert len(bc_events) == 0
