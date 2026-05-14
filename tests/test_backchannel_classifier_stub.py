"""BackchannelClassifier — isinstance Protocol check and construction stub (v0.1b Task 8).

Success criterion: isinstance(fake, BackchannelModel) is True; BackchannelClassifier
can be instantiated with a fake model — no model SDK imported.
"""

from companion_harness.backchannel_classifier import BackchannelClassifier, BackchannelModel
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.9


def test_fake_satisfies_protocol():
    assert isinstance(_FakeBackchannelModel(), BackchannelModel)


def test_backchannel_classifier_construction():
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    logger = EventLogger(sink, maxsize=256)
    classifier = BackchannelClassifier(
        model=_FakeBackchannelModel(),
        session_id="test-session",
        logger=logger,
    )
    assert isinstance(classifier, BackchannelClassifier)
