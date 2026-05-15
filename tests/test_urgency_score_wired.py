"""v0.1j Task 10 — UrgencyScorer seam tests.

Success criterion:
    pytest tests/test_urgency_score_wired.py -v
"""

import inspect

from companion_harness.urgency_scorer import (
    UrgencyScorer,
    _NullUrgencyScorer,
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


class _FakeUrgencyScorer:
    """Fake scorer that returns a fixed value so tests can assert the seam is live."""

    def __init__(self, value: float) -> None:
        self._value = value

    def score(self, transcript: str, audio: bytes | None) -> float:
        return self._value


def test_urgency_score_uses_scorer_accessor():
    """PolicyInputs.urgency_score is sourced from the injected scorer."""
    fake = _FakeUrgencyScorer(0.7)
    builder = _make_policy_inputs_builder(None, urgency_scorer=fake)
    inputs = builder(_signal(), [])
    assert inputs.urgency_score == 0.7


def test_null_urgency_scorer_returns_zero_with_marker():
    """_NullUrgencyScorer returns 0.0 and carries the UNAVAILABLE marker."""
    scorer = _NullUrgencyScorer()
    assert scorer.score("hello", b"audio") == 0.0
    assert scorer.score("", None) == 0.0
    # Marker discipline: source must contain the UNAVAILABLE comment referencing #171.
    src = inspect.getsource(_NullUrgencyScorer.score)
    assert "UNAVAILABLE: #171" in src


def test_urgency_scorer_protocol_runtime_checkable():
    """`isinstance` check works against the runtime-checkable Protocol."""
    assert isinstance(_NullUrgencyScorer(), UrgencyScorer)
