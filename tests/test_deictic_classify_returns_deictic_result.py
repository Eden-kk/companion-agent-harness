"""DeicticDetector.classify() → DeicticResult + is_ambiguous derivation (v0.1j Tasks 4+7)."""

import pytest

from companion_harness.deictic_detector import DeicticDetector, DeicticResult
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


class _FakeDeicticModel:
    def __init__(self, is_deictic: bool = True, confidence: float = 0.9) -> None:
        self._is_deictic = is_deictic
        self._confidence = confidence

    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]:
        return self._is_deictic, self._confidence


class _FakeDeicticModelWithTopK(_FakeDeicticModel):
    def __init__(self, candidates: list[tuple[str, float]], **kw) -> None:
        super().__init__(**kw)
        self._candidates = candidates

    def top_k(self, transcript: str, audio_buffer: bytes | None) -> list[tuple[str, float]]:
        return self._candidates


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


@pytest.mark.asyncio
async def test_classify_returns_deictic_result_not_tuple():
    logger, _ = _make_logger()
    await logger.start()
    detector = DeicticDetector(model=_FakeDeicticModel(), session_id="test-t4-a", logger=logger)
    result = detector.classify("what is this?", caused_by=["evt-1"])
    await logger.stop()
    assert isinstance(result, DeicticResult)
    assert not isinstance(result, tuple)


@pytest.mark.asyncio
async def test_classify_is_ambiguous_true_when_top_k_close():
    logger, _ = _make_logger()
    await logger.start()
    candidates = [("a", 0.9), ("b", 0.85)]  # diff = 0.05 < 0.1
    model = _FakeDeicticModelWithTopK(candidates=candidates, is_deictic=True)
    detector = DeicticDetector(model=model, session_id="test-t4-b", logger=logger)
    result = detector.classify("what is this?", caused_by=["evt-1"])
    await logger.stop()
    assert result.is_ambiguous is True
    assert result.candidates == candidates


@pytest.mark.asyncio
async def test_classify_is_ambiguous_false_when_top_k_far():
    logger, _ = _make_logger()
    await logger.start()
    candidates = [("a", 0.9), ("b", 0.2)]  # diff = 0.7 >= 0.1
    model = _FakeDeicticModelWithTopK(candidates=candidates, is_deictic=True)
    detector = DeicticDetector(model=model, session_id="test-t4-c", logger=logger)
    result = detector.classify("what is this?", caused_by=["evt-1"])
    await logger.stop()
    assert result.is_ambiguous is False


@pytest.mark.asyncio
async def test_classify_hasattr_guard_works_for_legacy_fakes():
    logger, _ = _make_logger()
    await logger.start()
    model = _FakeDeicticModel(is_deictic=False, confidence=0.8)
    detector = DeicticDetector(model=model, session_id="test-t4-d", logger=logger)
    result = detector.classify("what time is it?", caused_by=["evt-1"])
    await logger.stop()
    assert isinstance(result, DeicticResult)
    assert result.candidates == []
    assert result.is_ambiguous is False
