"""DeicticDetector — stub unit tests (v0.1c Task 5).

Task 5 success criterion: deictic_detector.py imports cleanly; the isinstance
Protocol check passes with a fake DeicticModel.
"""

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


def test_fake_satisfies_protocol():
    assert isinstance(_FakeDeicticModel([(True, 0.9)]), DeicticModel)


def test_deictic_detector_construction():
    logger, _ = _make_logger()
    detector = DeicticDetector(
        model=_FakeDeicticModel([(True, 0.9)]),
        session_id="test-session",
        logger=logger,
    )
    assert isinstance(detector, DeicticDetector)


def test_classify_returns_model_result():
    logger, _ = _make_logger()
    detector = DeicticDetector(
        model=_FakeDeicticModel([(True, 0.92)]),
        session_id="test-task5",
        logger=logger,
    )
    is_deictic, confidence = detector.classify(
        transcript="what is this?",
        caused_by=["upstream-event-001"],
    )
    assert is_deictic is True
    assert confidence == 0.92
