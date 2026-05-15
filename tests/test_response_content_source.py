"""v0.1h Task 3 — SpeakDecision.response_content_source field.

Contract: every decide() return path sets response_content_source explicitly;
default is "no_synthesis" (invariant-#8-safe). Replay determinism is unchanged.
"""

import pytest

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs, SpeakDecision
from companion_harness.speak_policy import decide


def _base_inputs(**overrides) -> PolicyInputs:
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
    d = SpeakDecision(
        action_type="silence",
        primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
        supporting_reason_codes=[],
        redacted_explanation=None,
        caused_by=["sig-0"],
        budget_bucket=None,
        allowed_prosody_tags=[],
        max_duration_ms=None,
    )
    assert d.response_content_source == "no_synthesis"


# Parametric: one fixture per decide() path, expected source per §3 table.
@pytest.mark.parametrize("inputs_kwargs,decide_kwargs,expected_action,expected_source", [
    # silence — social_mode block
    (
        {"social_mode": "group_conversation"},
        {},
        "silence",
        "no_synthesis",
    ),
    # silence — user_speaking
    (
        {"user_speaking": True},
        {},
        "silence",
        "no_synthesis",
    ),
    # silence — eou below threshold
    (
        {"eou_probability": 0.5},
        {},
        "silence",
        "no_synthesis",
    ),
    # silence — not addressed (fallthrough)
    (
        {"user_addressed_agent": False},
        {},
        "silence",
        "no_synthesis",
    ),
    # alert
    (
        {"current_task_mode": "cooking", "urgency_score": 0.5},
        {},
        "alert",
        "template_alert",
    ),
    # backchannel
    (
        {"user_addressed_agent": True},
        {"p_backchannel": 0.8},
        "backchannel",
        "template_backchannel",
    ),
    # clarification — audio-visual conflict
    (
        {"audio_visual_conflict_score": 0.9, "user_addressed_agent": True},
        {},
        "clarification",
        "foreground_response_proposal",
    ),
    # clarification — deictic ambiguous
    (
        {"deictic_reference": True, "deictic_ambiguous": True, "grounding_confidence": 0.9},
        {},
        "clarification",
        "foreground_response_proposal",
    ),
    # short_reaction
    (
        {
            "short_response_appropriate": True,
            "proactivity_budget_remaining": {"short_reaction": 1},
            "user_addressed_agent": False,
        },
        {},
        "short_reaction",
        "foreground_response_proposal",
    ),
    # full_response
    (
        {"user_addressed_agent": True},
        {},
        "full_response",
        "foreground_response_proposal",
    ),
    # aesthetic_reaction
    (
        {"user_addressed_agent": False, "aesthetic_novelty_score": 0.6},
        {},
        "aesthetic_reaction",
        "thinker_proposal",
    ),
])
def test_every_decide_path_sets_response_content_source(
    inputs_kwargs, decide_kwargs, expected_action, expected_source
):
    inputs = _base_inputs(**inputs_kwargs)
    decision = decide(inputs, ["sig-1"], **decide_kwargs)
    assert decision.action_type == expected_action, (
        f"expected action_type={expected_action!r}, got {decision.action_type!r}"
    )
    assert decision.response_content_source == expected_source, (
        f"action={expected_action!r}: expected source={expected_source!r}, "
        f"got {decision.response_content_source!r}"
    )


def test_response_content_source_populated_rate():
    """Backstop contract: 100% of decide() returns have response_content_source set (not None)."""
    fixtures = [
        (_base_inputs(social_mode="group_conversation"), {}),
        (_base_inputs(user_speaking=True), {}),
        (_base_inputs(eou_probability=0.5), {}),
        (_base_inputs(current_task_mode="cooking", urgency_score=0.5), {}),
        (_base_inputs(user_addressed_agent=True), {"p_backchannel": 0.8}),
        (_base_inputs(audio_visual_conflict_score=0.9), {}),
        (_base_inputs(deictic_reference=True, deictic_ambiguous=True, grounding_confidence=0.9), {}),
        (_base_inputs(short_response_appropriate=True, proactivity_budget_remaining={"short_reaction": 1}, user_addressed_agent=False), {}),
        (_base_inputs(user_addressed_agent=True), {}),
        (_base_inputs(user_addressed_agent=False, aesthetic_novelty_score=0.6), {}),
        (_base_inputs(user_addressed_agent=False), {}),
    ]
    for inputs, kwargs in fixtures:
        decision = decide(inputs, ["sig-1"], **kwargs)
        assert decision.response_content_source is not None
        assert decision.response_content_source != ""
