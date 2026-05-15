"""v0.1j Task 6 — AudioVisualConflictScorer seam tests.

Success criterion:
    pytest tests/test_audio_visual_conflict_score_wired.py -v
"""

import inspect

from companion_harness.av_conflict_scorer import (
    AudioVisualConflictScorer,
    _NullAudioVisualConflictScorer,
)
from manual_test_console.live_pipeline import (
    _make_policy_inputs_builder,
)
from companion_harness.schemas import TurnSignal


def _signal(p_done: float = 0.8) -> TurnSignal:
    return TurnSignal(
        detector="test",
        p_done=p_done,
        p_continue=1.0 - p_done,
        p_backchannel=0.0,
        confidence=1.0,
        evidence_event_ids=[],
    )


class _FakeAVScorer:
    """Fake scorer that returns a fixed value so tests can assert the seam is live."""

    def __init__(self, value: float) -> None:
        self._value = value

    def score(self, audio: bytes, frame: bytes | None) -> float:
        return self._value


def test_audio_visual_conflict_score_uses_scorer_accessor():
    """PolicyInputs.audio_visual_conflict_score is sourced from the injected scorer."""
    fake = _FakeAVScorer(0.8)
    builder = _make_policy_inputs_builder(None, av_scorer=fake)
    inputs = builder(_signal(), [])
    assert inputs.audio_visual_conflict_score == 0.8


def test_null_av_conflict_scorer_returns_zero_with_marker():
    """_NullAudioVisualConflictScorer returns 0.0 and carries the UNAVAILABLE marker."""
    scorer = _NullAudioVisualConflictScorer()
    assert scorer.score(b"audio", b"frame") == 0.0
    assert scorer.score(b"audio", None) == 0.0
    # Marker discipline: source must contain the UNAVAILABLE comment referencing #168.
    src = inspect.getsource(_NullAudioVisualConflictScorer.score)
    assert "UNAVAILABLE: #168" in src


def test_av_conflict_protocol_runtime_checkable():
    """`isinstance` check works against the runtime-checkable Protocol."""
    assert isinstance(_NullAudioVisualConflictScorer(), AudioVisualConflictScorer)
