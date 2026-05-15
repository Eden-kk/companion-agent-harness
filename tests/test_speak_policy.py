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


def test_backchannel_high_p_backchannel():
    """EOU confirmed + p_backchannel >= 0.7 → backchannel with BACKCHANNEL_DETECTED."""
    result = decide(_inputs(eou_probability=0.9, user_addressed_agent=True), ["sig-1"], p_backchannel=0.7)
    assert result.action_type == "backchannel"
    assert result.primary_reason_code == ReasonCode.BACKCHANNEL_DETECTED


def test_backchannel_below_threshold_still_full_response():
    """p_backchannel=0.69 is below threshold — full_response path is not suppressed."""
    result = decide(_inputs(eou_probability=0.9, user_addressed_agent=True), ["sig-1"], p_backchannel=0.69)
    assert result.action_type == "full_response"
    assert result.primary_reason_code == ReasonCode.EOU_CONFIRMED


def test_backchannel_determinism():
    """Identical inputs + p_backchannel → bit-identical SpeakDecision (invariant #5)."""
    inputs = _inputs(eou_probability=0.9, user_addressed_agent=True)
    d1 = decide(inputs, ["sig-1"], p_backchannel=0.85)
    d2 = decide(inputs, ["sig-1"], p_backchannel=0.85)
    assert d1 == d2
    assert d1.action_type == "backchannel"


def test_audio_visual_conflict_high_score():
    """audio_visual_conflict_score > 0.7 → clarification with AUDIO_VISUAL_CONFLICT, not full_response."""
    result = decide(_inputs(audio_visual_conflict_score=0.9, user_addressed_agent=True), ["sig-1"])
    assert result.primary_reason_code == ReasonCode.AUDIO_VISUAL_CONFLICT
    assert result.action_type != "full_response"
    assert result.action_type == "clarification"


def test_audio_visual_conflict_determinism():
    """Identical inputs with high conflict score → bit-identical SpeakDecision (invariant #5)."""
    inputs = _inputs(audio_visual_conflict_score=0.9, user_addressed_agent=True)
    d1 = decide(inputs, ["sig-1"])
    d2 = decide(inputs, ["sig-1"])
    assert d1 == d2
    assert d1.primary_reason_code == ReasonCode.AUDIO_VISUAL_CONFLICT


def test_audio_visual_conflict_below_threshold_unaffected():
    """audio_visual_conflict_score <= 0.7 does not trigger conflict branch."""
    result = decide(_inputs(audio_visual_conflict_score=0.7, user_addressed_agent=True), ["sig-1"])
    assert result.action_type == "full_response"
    assert result.primary_reason_code == ReasonCode.EOU_CONFIRMED


def test_audio_visual_conflict_default_zero():
    """PolicyInputs default audio_visual_conflict_score=0.0 — existing tests unaffected."""
    inputs = _inputs()
    assert inputs.audio_visual_conflict_score == 0.0


# Task 7 — alert-threshold gate tests

def test_alert_cooking_urgency_above_low_threshold():
    """cooking mode + urgency_score=0.5 > low threshold (0.3) → alert with ALERT_THRESHOLD_EXCEEDED."""
    result = decide(_inputs(current_task_mode="cooking", urgency_score=0.5), ["sig-1"])
    assert result.action_type == "alert"
    assert result.primary_reason_code == ReasonCode.ALERT_THRESHOLD_EXCEEDED


def test_alert_creative_focus_urgency_below_high_threshold():
    """creative_focus + urgency_score=0.4 < high threshold (0.85) → does NOT fire alert (falls through)."""
    result = decide(
        _inputs(current_task_mode="creative_focus", urgency_score=0.4, user_addressed_agent=True),
        ["sig-1"],
    )
    assert result.action_type != "alert"


def test_alert_determinism():
    """Same inputs → bit-identical SpeakDecision for alert branch (invariant #5)."""
    inputs = _inputs(current_task_mode="cooking", urgency_score=0.5)
    d1 = decide(inputs, ["sig-1"])
    d2 = decide(inputs, ["sig-1"])
    assert d1 == d2
    assert d1.action_type == "alert"


# Task 9a — short_reaction path

def test_short_reaction_trigger_with_budget():
    """short_response_appropriate=True + budget available → short_reaction with PROACTIVITY_BUDGET_AVAILABLE."""
    result = decide(
        _inputs(
            short_response_appropriate=True,
            proactivity_budget_remaining={"short_reaction": 1},
            user_addressed_agent=False,
        ),
        ["sig-1"],
    )
    assert result.action_type == "short_reaction"
    assert result.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE


def test_short_reaction_budget_exhausted_yields_silence():
    """short_response_appropriate=True but budget=0 → silence (budget gate, not trigger gate)."""
    result = decide(
        _inputs(
            short_response_appropriate=True,
            proactivity_budget_remaining={"short_reaction": 0},
            user_addressed_agent=False,
        ),
        ["sig-1"],
    )
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.COOLDOWN_BLOCKED


def test_short_reaction_absent_trigger_falls_through_to_full_response():
    """short_response_appropriate=False + user_addressed_agent=True → full_response, not short_reaction."""
    result = decide(
        _inputs(
            short_response_appropriate=False,
            proactivity_budget_remaining={"short_reaction": 1},
            user_addressed_agent=True,
        ),
        ["sig-1"],
    )
    assert result.action_type == "full_response"
    assert result.primary_reason_code == ReasonCode.EOU_CONFIRMED


def test_short_reaction_determinism():
    """Identical inputs → bit-identical SpeakDecision (invariant #5)."""
    inputs = _inputs(
        short_response_appropriate=True,
        proactivity_budget_remaining={"short_reaction": 2},
        user_addressed_agent=False,
    )
    d1 = decide(inputs, ["sig-1"])
    d2 = decide(inputs, ["sig-1"])
    assert d1 == d2
    assert d1.action_type == "short_reaction"


# Aesthetic reaction gate tests (Task 8)

def _aesthetic_inputs(**overrides):
    """Base inputs that reach the aesthetic_reaction gate: agent not addressed, high novelty."""
    return _inputs(user_addressed_agent=False, aesthetic_novelty_score=0.6, **overrides)


def test_aesthetic_reaction_fires():
    """High novelty + normal mode + no cooldown → aesthetic_reaction / PROACTIVITY_BUDGET_AVAILABLE."""
    result = decide(_aesthetic_inputs(), ["sig-1"])
    assert result.action_type == "aesthetic_reaction"
    assert result.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE


def test_aesthetic_reaction_blocked_creative_focus():
    """High novelty + creative_focus mode → silence / QUIET_MODE_BLOCKED."""
    result = decide(_aesthetic_inputs(current_task_mode="creative_focus"), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.QUIET_MODE_BLOCKED


def test_aesthetic_reaction_blocked_cooldown():
    """High novelty + cooldown_state["aesthetic_reaction"] > 0 → silence / COOLDOWN_BLOCKED."""
    result = decide(_aesthetic_inputs(cooldown_state={"aesthetic_reaction": 3}), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.COOLDOWN_BLOCKED


def test_aesthetic_reaction_blocked_quiet_mode():
    """High novelty + quiet_mode_active=True → silence / QUIET_MODE_BLOCKED."""
    result = decide(_aesthetic_inputs(quiet_mode_active=True), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.QUIET_MODE_BLOCKED


def test_aesthetic_reaction_blocked_crisis_emergency():
    """crisis_emergency mode → treated as disabled (no aesthetic_reaction)."""
    result = decide(_aesthetic_inputs(current_task_mode="crisis_emergency"), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.QUIET_MODE_BLOCKED
