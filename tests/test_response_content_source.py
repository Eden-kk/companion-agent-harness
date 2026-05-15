"""Tests for SpeakDecision.response_content_source field (v0.1h Task 3).

Success criterion: every decide() return path carries a non-default
response_content_source for non-silence actions; silence always
carries "no_synthesis".
"""

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


def test_speak_decision_default_value_is_no_synthesis():
    decision = SpeakDecision(
        action_type="silence",
        primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
        supporting_reason_codes=[],
        redacted_explanation=None,
        caused_by=["sig-0"],
        budget_bucket=None,
        allowed_prosody_tags=[],
        max_duration_ms=None,
    )
    assert decision.response_content_source == "no_synthesis"


def test_silence_path_no_synthesis():
    result = decide(_inputs(social_mode="group_conversation"), ["sig-1"])
    assert result.action_type == "silence"
    assert result.response_content_source == "no_synthesis"


def test_silence_eou_tie_no_synthesis():
    result = decide(_inputs(eou_probability=0.5), ["sig-1"])
    assert result.action_type == "silence"
    assert result.response_content_source == "no_synthesis"


def test_silence_user_speaking_no_synthesis():
    result = decide(_inputs(user_speaking=True), ["sig-1"])
    assert result.action_type == "silence"
    assert result.response_content_source == "no_synthesis"


def test_full_response_foreground_response_proposal():
    result = decide(_inputs(eou_probability=0.9, user_addressed_agent=True), ["sig-1"])
    assert result.action_type == "full_response"
    assert result.response_content_source == "foreground_response_proposal"


def test_backchannel_backchannel_source():
    result = decide(
        _inputs(eou_probability=0.9, user_addressed_agent=True),
        ["sig-1"],
        p_backchannel=0.8,
    )
    assert result.action_type == "backchannel"
    assert result.response_content_source == "backchannel"


def test_alert_foreground_response_proposal():
    result = decide(_inputs(current_task_mode="cooking", urgency_score=0.5), ["sig-1"])
    assert result.action_type == "alert"
    assert result.response_content_source == "foreground_response_proposal"


def test_clarification_audio_visual_conflict_foreground_response_proposal():
    result = decide(_inputs(audio_visual_conflict_score=0.9, user_addressed_agent=True), ["sig-1"])
    assert result.action_type == "clarification"
    assert result.response_content_source == "foreground_response_proposal"


def test_clarification_deictic_ambiguous_foreground_response_proposal():
    result = decide(
        _inputs(deictic_reference=True, deictic_ambiguous=True, grounding_confidence=0.9),
        ["sig-1"],
    )
    assert result.action_type == "clarification"
    assert result.response_content_source == "foreground_response_proposal"


def test_short_reaction_foreground_response_proposal():
    result = decide(
        _inputs(
            short_response_appropriate=True,
            proactivity_budget_remaining={"short_reaction": 1},
            user_addressed_agent=False,
        ),
        ["sig-1"],
    )
    assert result.action_type == "short_reaction"
    assert result.response_content_source == "foreground_response_proposal"


def test_aesthetic_reaction_foreground_response_proposal():
    result = decide(
        _inputs(user_addressed_agent=False, aesthetic_novelty_score=0.6),
        ["sig-1"],
    )
    assert result.action_type == "aesthetic_reaction"
    assert result.response_content_source == "foreground_response_proposal"


def test_response_content_source_populated_rate():
    """All non-silence decide() paths have a non-default response_content_source."""
    cases = [
        # (inputs_kwargs, p_backchannel, expected_action, expected_source)
        ({"eou_probability": 0.9, "user_addressed_agent": True}, 0.0, "full_response", "foreground_response_proposal"),
        ({"eou_probability": 0.9, "user_addressed_agent": True}, 0.8, "backchannel", "backchannel"),
        ({"current_task_mode": "cooking", "urgency_score": 0.5}, 0.0, "alert", "foreground_response_proposal"),
        ({"audio_visual_conflict_score": 0.9, "user_addressed_agent": True}, 0.0, "clarification", "foreground_response_proposal"),
        ({"deictic_reference": True, "deictic_ambiguous": True, "grounding_confidence": 0.9}, 0.0, "clarification", "foreground_response_proposal"),
        ({"short_response_appropriate": True, "proactivity_budget_remaining": {"short_reaction": 1}, "user_addressed_agent": False}, 0.0, "short_reaction", "foreground_response_proposal"),
        ({"user_addressed_agent": False, "aesthetic_novelty_score": 0.6}, 0.0, "aesthetic_reaction", "foreground_response_proposal"),
        ({"social_mode": "group_conversation"}, 0.0, "silence", "no_synthesis"),
    ]
    for kwargs, p_bc, expected_action, expected_source in cases:
        result = decide(_inputs(**kwargs), ["sig-1"], p_backchannel=p_bc)
        assert result.action_type == expected_action, f"expected {expected_action}, got {result.action_type}"
        assert result.response_content_source == expected_source, (
            f"action={expected_action}: expected source={expected_source!r}, "
            f"got {result.response_content_source!r}"
        )
