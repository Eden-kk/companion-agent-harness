"""DeicticDetector — stub unit tests (v0.1c Task 5).

Task 5 success criterion: deictic_detector.py imports cleanly; the isinstance
Protocol check passes with a fake DeicticModel.
"""

from companion_harness.deictic_detector import DeicticDetector, DeicticModel
from companion_harness.event_logger import EventLogger


class _FakeDeicticModel:
    """Scripted model: returns the next (is_deictic, confidence) from the sequence."""

    def __init__(self, results: list[tuple[bool, float]]) -> None:
        self._results = iter(results)

    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]:
        return next(self._results)


def test_fake_satisfies_protocol():
    assert isinstance(_FakeDeicticModel([(True, 0.9)]), DeicticModel)


def test_deictic_detector_construction():
    async def _sink(event):
        pass

    logger = EventLogger(_sink, maxsize=256)
    detector = DeicticDetector(
        model=_FakeDeicticModel([(True, 0.9)]),
        session_id="test-session",
        logger=logger,
    )
    assert isinstance(detector, DeicticDetector)
