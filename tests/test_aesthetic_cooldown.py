"""Aesthetic cooldown fixture replay — v0.1d Task 14.

Success criterion: cooldown_state={"aesthetic_reaction": 0} fires aesthetic_reaction;
cooldown_state={"aesthetic_reaction": 1} is blocked with COOLDOWN_BLOCKED.
Spec line 505: at most one expressive reaction within the cooldown window.
"""

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness import speak_policy


def _inputs_from_frame(frame: dict) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=frame["user_speaking"],
        eou_probability=frame["eou_probability"],
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=frame["user_addressed_agent"],
        urgency_score=frame["urgency_score"],
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode=frame["current_task_mode"],
        social_mode=frame["social_mode"],
        risk_mode="normal",
        cooldown_state=frame["cooldown_state"],
        attachment_risk_level=0.0,
        aesthetic_novelty_score=frame["aesthetic_novelty_score"],
        short_response_appropriate=frame["short_response_appropriate"],
    )


def test_aesthetic_cooldown_fires_when_clear():
    """Sub-case 1: cooldown_state={"aesthetic_reaction": 0} → aesthetic_reaction / PROACTIVITY_BUDGET_AVAILABLE."""
    fixture = load_fixture("aesthetic_cooldown_001")
    resolution = next(f for f in fixture["signal_trace"] if f.get("resolution_frame") and f["sub_case"] == 1)
    result = speak_policy.decide(_inputs_from_frame(resolution), [resolution["frame_id"]])
    assert result.action_type == "aesthetic_reaction"
    assert result.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE


def test_aesthetic_cooldown_blocked_when_consumed():
    """Sub-case 2: cooldown_state={"aesthetic_reaction": 1} → silence / COOLDOWN_BLOCKED."""
    fixture = load_fixture("aesthetic_cooldown_001")
    resolution = next(f for f in fixture["signal_trace"] if f.get("resolution_frame") and f["sub_case"] == 2)
    result = speak_policy.decide(_inputs_from_frame(resolution), [resolution["frame_id"]])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.COOLDOWN_BLOCKED
