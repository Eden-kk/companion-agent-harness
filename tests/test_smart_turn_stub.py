"""SmartTurnDetector — isinstance Protocol check and construction stub (v0.1b Task 2).

Success criterion: isinstance(fake, SmartTurnModel) is True; SmartTurnDetector
can be instantiated with a fake model — no model SDK imported.
"""

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event
from companion_harness.turn_detector_smart import SmartTurnDetector, SmartTurnModel


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


def test_fake_satisfies_protocol():
    assert isinstance(_FakeSmartTurnModel(), SmartTurnModel)


def test_smart_turn_detector_construction():
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    logger = EventLogger(sink, maxsize=256)
    detector = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id="test-session",
        logger=logger,
    )
    assert detector is not None
