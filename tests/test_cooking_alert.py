"""Stage 3 — per-mode alert threshold discriminator.

Fixture cooking_alert_001 has two sub-cases sharing urgency_score=0.5:
  - cooking mode: threshold 'low' = 0.3; 0.5 > 0.3 → alert fires (ALERT_THRESHOLD_EXCEEDED).
  - normal mode:  threshold 'medium' = 0.6; 0.5 < 0.6 → gate 1b passes; eou_probability=0.3
    falls through gate 3 → silence (NOT_ADDRESSED_TO_AGENT).

This contrast proves the per-mode threshold mapping in speak_policy_config is wired correctly.
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
        user_addressed_agent=False,
        urgency_score=frame["urgency_score"],
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode=frame["current_task_mode"],
        social_mode=frame["social_mode"],
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


def test_cooking_alert():
    """cooking mode + urgency_score=0.5 > 0.3 (low threshold) → alert / ALERT_THRESHOLD_EXCEEDED."""
    fixture = load_fixture("cooking_alert_001")
    assert fixture["case_id"] == "cooking_alert_001"
    assert fixture["expected_metrics"]["alert_response_rate"] == 1.0

    resolution_frame = next(
        f for f in fixture["signal_trace"]
        if f.get("resolution_frame") and f["sub_case"] == "cooking"
    )
    inputs = _inputs_from_frame(resolution_frame)
    decision = speak_policy.decide(inputs, signal_event_ids=["cooking-alert-signal-001"])

    assert decision.action_type == "alert", (
        f"expected alert, got {decision.action_type!r} "
        f"(urgency={resolution_frame['urgency_score']}, mode={resolution_frame['current_task_mode']})"
    )
    assert decision.primary_reason_code == ReasonCode.ALERT_THRESHOLD_EXCEEDED


def test_normal_no_alert():
    """normal mode + urgency_score=0.5 < 0.6 (medium threshold) → gate 1b passes → silence."""
    fixture = load_fixture("cooking_alert_001")

    resolution_frame = next(
        f for f in fixture["signal_trace"]
        if f.get("resolution_frame") and f["sub_case"] == "normal"
    )
    inputs = _inputs_from_frame(resolution_frame)
    decision = speak_policy.decide(inputs, signal_event_ids=["normal-noalert-signal-001"])

    assert decision.action_type != "alert", (
        f"alert must NOT fire in normal mode at urgency=0.5 "
        f"(threshold=0.6); got {decision.action_type!r}"
    )
    assert decision.action_type == "silence"
    assert decision.primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT
