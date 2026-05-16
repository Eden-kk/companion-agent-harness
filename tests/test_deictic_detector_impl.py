"""DeicticDetector — implementation tests (v0.1c Task 6).

Task 6 success criterion: pytest -k deictic passes non-vacuously — a scripted
"what is this?" trace produces deictic_reference=True, a scripted "what time is
it?" trace produces deictic_reference=False, and every model invocation is
logged as an Event with non-empty caused_by[] (verified via a real async-logger
test).
"""

import pytest

from companion_harness.deictic_detector import DeicticDetector, DeicticModel
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


class _FakeDeicticModel:
    """Scripted model: returns the next (is_deictic, confidence) from the sequence."""

    def __init__(self, results: list[tuple[bool, float]]) -> None:
        self._results = iter(results)

    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]:
        return next(self._results)


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


@pytest.mark.asyncio
async def test_deictic_utterance_produces_true():
    """'what is this?' → deictic_reference=True, event logged with non-empty caused_by."""
    logger, received = _make_logger()
    await logger.start()

    model = _FakeDeicticModel([(True, 0.91)])
    detector = DeicticDetector(model=model, session_id="test-task6-a", logger=logger)

    cause = ["upstream-asr-event-001"]
    result = detector.classify("what is this?", caused_by=cause)

    await logger.stop()

    assert result.is_deictic is True
    assert result.confidence == pytest.approx(0.91)

    assert len(received) == 1
    evt = received[0]
    assert evt.event_type == "deictic_classification"
    assert evt.caused_by == cause
    assert result.event_id == evt.event_id


@pytest.mark.asyncio
async def test_non_deictic_utterance_produces_false():
    """'what time is it?' → deictic_reference=False, event logged with non-empty caused_by."""
    logger, received = _make_logger()
    await logger.start()

    model = _FakeDeicticModel([(False, 0.88)])
    detector = DeicticDetector(model=model, session_id="test-task6-b", logger=logger)

    cause = ["upstream-asr-event-002"]
    result = detector.classify("what time is it?", caused_by=cause)

    await logger.stop()

    assert result.is_deictic is False
    assert result.confidence == pytest.approx(0.88)

    assert len(received) == 1
    evt = received[0]
    assert evt.event_type == "deictic_classification"
    assert evt.caused_by == cause
    assert result.event_id == evt.event_id


@pytest.mark.asyncio
async def test_each_invocation_logs_one_event():
    """Multiple classify() calls each log exactly one event."""
    logger, received = _make_logger()
    await logger.start()

    model = _FakeDeicticModel([(True, 0.9), (False, 0.85)])
    detector = DeicticDetector(model=model, session_id="test-task6-c", logger=logger)

    cause = ["evt-100"]
    r0 = detector.classify("look at this", caused_by=cause)
    r1 = detector.classify("what is the weather?", caused_by=cause)

    await logger.stop()

    assert len(received) == 2
    assert all(e.event_type == "deictic_classification" for e in received)
    assert all(e.caused_by == cause for e in received)
    assert r0.event_id == received[0].event_id
    assert r1.event_id == received[1].event_id


@pytest.mark.asyncio
async def test_classify_raises_on_empty_caused_by():
    """classify() with caused_by=[] raises ValueError (orphan-event guard)."""
    logger, _ = _make_logger()
    await logger.start()

    model = _FakeDeicticModel([(True, 0.9)])
    detector = DeicticDetector(model=model, session_id="test-task6-d", logger=logger)

    with pytest.raises(ValueError):
        detector.classify("look at this", caused_by=[])

    await logger.stop()
