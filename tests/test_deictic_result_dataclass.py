"""DeicticResult dataclass field and immutability tests (v0.1j Tasks 4+7)."""

import dataclasses

from companion_harness.deictic_detector import DeicticResult


def test_deictic_result_has_5_fields():
    fields = {f.name for f in dataclasses.fields(DeicticResult)}
    assert fields == {"is_deictic", "confidence", "is_ambiguous", "candidates", "event_id"}


def test_deictic_result_is_frozen():
    r = DeicticResult(
        is_deictic=True,
        confidence=0.9,
        is_ambiguous=False,
        candidates=[],
        event_id="test-evt-1",
    )
    try:
        r.is_deictic = False  # type: ignore[misc]
        raise AssertionError("should have raised FrozenInstanceError")
    except dataclasses.FrozenInstanceError:
        pass
