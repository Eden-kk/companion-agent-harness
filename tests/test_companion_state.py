"""Unit tests for companion_state schema (Stage 4 scaffolding Task 5).

Success criterion (verbatim):
  companion_state.py imports cleanly; constructing a CompanionState with
  SensitiveField free-text fields works; a unit test asserts that constructing
  the dataclass with a raw str in a SensitiveField slot is rejected (type-checking
  it via runtime guard if dataclasses don't validate); grep confirms no mutation
  path / no state machine is added.
"""

import pytest

from companion_harness.companion_state import (
    AffectiveHypothesis,
    CompanionState,
    EngagementState,
    RecentSharedMoment,
    StyleState,
    YesterdaySummary,
)
from companion_harness.schemas import SensitiveField


def _sf(value: str) -> SensitiveField:
    return SensitiveField(retention_policy_id="default", value=value)


def _make_state() -> CompanionState:
    return CompanionState(
        style_state=StyleState(
            label="quiet_warm",
            source="policy_default",
            confidence=0.9,
            expires_after_turns=10,
        ),
        engagement_state=EngagementState(
            current_interest=_sf("bird on the windowsill"),
            reason=_sf("user pointed camera at it"),
            allowed_actions=("aesthetic_reaction", "silence"),
        ),
        affective_hypotheses=(
            AffectiveHypothesis(
                hypothesis=_sf("user may be tired"),
                evidence=_sf("slow typing cadence"),
                confidence=0.4,
                allowed_to_surface=False,
            ),
        ),
        recent_shared_moments=(
            RecentSharedMoment(
                event_id="episodic-001",
                salience=0.8,
                last_referenced_at="2026-05-14T08:00:00Z",
            ),
        ),
        yesterday_summary=YesterdaySummary(
            content=_sf("walked through the park, saw ducks"),
            emotional_weight="medium",
            should_surface=True,
        ),
    )


def test_companion_state_constructs():
    state = _make_state()
    assert isinstance(state, CompanionState)


def test_style_state_fields():
    state = _make_state()
    assert state.style_state.label == "quiet_warm"
    assert state.style_state.confidence == pytest.approx(0.9)


def test_engagement_state_fields_are_sensitive_field():
    state = _make_state()
    assert isinstance(state.engagement_state.current_interest, SensitiveField)
    assert isinstance(state.engagement_state.reason, SensitiveField)


def test_affective_hypothesis_fields_are_sensitive_field():
    state = _make_state()
    hyp = state.affective_hypotheses[0]
    assert isinstance(hyp.hypothesis, SensitiveField)
    assert isinstance(hyp.evidence, SensitiveField)


def test_yesterday_summary_content_is_sensitive_field():
    state = _make_state()
    assert isinstance(state.yesterday_summary.content, SensitiveField)


def test_raw_str_in_sensitive_field_slot_rejected():
    """A raw str passed where SensitiveField is required must be rejected at runtime."""
    with pytest.raises(TypeError):
        EngagementState(
            current_interest="raw string — should be rejected",  # type: ignore[arg-type]
            reason=_sf("ok"),
            allowed_actions=(),
        )


def test_frozen_dataclass_immutable():
    state = _make_state()
    with pytest.raises((AttributeError, TypeError)):
        state.style_state = StyleState(  # type: ignore[misc]
            label="playful",
            source="user_pref",
            confidence=0.5,
            expires_after_turns=5,
        )
