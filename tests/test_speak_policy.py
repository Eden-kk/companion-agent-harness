"""SpeakPolicy unit tests — determinism, tie-breaking, social-mode block, orphan guard.

Success criterion (ROADMAP Task 6 review): the five cases below pass and the
decide() function satisfies invariant #5 (determinism) and invariant #8
(silence wins ties).  No privacy-mode block case: spec Part 7 does not
designate any privacy_mode value as speech-blocking for v0.1a, so
_BLOCKING_PRIVACY_MODES is empty.
"""

import pytest

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs, SpeakDecision
from companion_harness.speak_policy import decide


def _inputs(**overrides) -> PolicyInputs:
    defaults = dict(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )
    defaults.update(overrides)
    return PolicyInputs(**defaults)


def test_full_response():
    """EOU confirmed + agent addressed → full_response with EOU_CONFIRMED."""
    result = decide(_inputs(eou_probability=0.9, user_addressed_agent=True), ["sig-1"])
    assert result.action_type == "full_response"
    assert result.primary_reason_code == ReasonCode.EOU_CONFIRMED


def test_silence_at_tie():
    """eou_probability=0.5 is a tie; silence wins (invariant #8)."""
    result = decide(_inputs(eou_probability=0.5), ["sig-1"])
    assert result.action_type == "silence"


def test_social_mode_block():
    """group_conversation social mode → silence with NOT_ADDRESSED_TO_AGENT."""
    result = decide(_inputs(social_mode="group_conversation"), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT


def test_determinism():
    """Identical PolicyInputs → bit-identical SpeakDecision (invariant #5)."""
    inputs = _inputs(eou_probability=0.9, user_addressed_agent=True)
    d1 = decide(inputs, ["sig-1", "sig-2"])
    d2 = decide(inputs, ["sig-1", "sig-2"])
    assert d1 == d2


def test_empty_signal_event_ids_raises():
    """Empty signal_event_ids must raise ValueError — orphan decisions fail Stage 0."""
    with pytest.raises(ValueError):
        decide(_inputs(), [])
