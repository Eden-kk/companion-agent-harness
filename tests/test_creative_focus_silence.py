"""Stage 3 contract test — creative_focus mode silences aesthetic_reaction (v0.1d Task 13).

Success criterion (verbatim):
  pytest tests/test_creative_focus_silence.py -v passes both sub-cases:
  creative_focus -> silence/QUIET_MODE_BLOCKED;
  normal -> aesthetic_reaction/PROACTIVITY_BUDGET_AVAILABLE.
  Specifically asserts QUIET_MODE_BLOCKED, NOT COOLDOWN_BLOCKED.
"""

import pytest

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness.speak_policy import decide

FIXTURE_ID = "creative_focus_silence_001"


@pytest.fixture(scope="module")
def fixture():
    return load_fixture(FIXTURE_ID)


@pytest.fixture(scope="module")
def creative_focus_frame(fixture):
    return next(
        s for s in fixture["signal_trace"]
        if s["sub_case"] == "creative_focus_suppression" and s.get("resolution_frame")
    )


@pytest.fixture(scope="module")
def normal_mode_frame(fixture):
    return next(
        s for s in fixture["signal_trace"]
        if s["sub_case"] == "normal_mode_fires" and s.get("resolution_frame")
    )


def _inputs_from_frame(frame) -> PolicyInputs:
    p = frame["policy_inputs"]
    return PolicyInputs(
        user_speaking=p["user_speaking"],
        eou_probability=p["eou_probability"],
        assistant_speaking=p["assistant_speaking"],
        scene_change_score=p["scene_change_score"],
        deictic_reference=p["deictic_reference"],
        user_addressed_agent=p["user_addressed_agent"],
        urgency_score=p["urgency_score"],
        proactivity_budget_remaining=p["proactivity_budget_remaining"],
        privacy_mode=p["privacy_mode"],
        current_task_mode=p["current_task_mode"],
        social_mode=p["social_mode"],
        risk_mode=p["risk_mode"],
        cooldown_state=p["cooldown_state"],
        attachment_risk_level=p["attachment_risk_level"],
        aesthetic_novelty_score=p["aesthetic_novelty_score"],
        short_response_appropriate=p["short_response_appropriate"],
        quiet_mode_active=p["quiet_mode_active"],
    )


def test_creative_focus_produces_silence_quiet_mode_blocked(creative_focus_frame):
    """creative_focus + quiet_mode_active=True + aesthetic_novelty=0.7 -> silence/QUIET_MODE_BLOCKED."""
    decision = decide(_inputs_from_frame(creative_focus_frame), [creative_focus_frame["frame_id"]])

    assert decision.action_type == "silence"
    assert decision.primary_reason_code == ReasonCode.QUIET_MODE_BLOCKED
    assert decision.primary_reason_code != ReasonCode.COOLDOWN_BLOCKED


def test_normal_mode_produces_aesthetic_reaction(normal_mode_frame):
    """normal mode + quiet_mode_active=False + aesthetic_novelty=0.7 -> aesthetic_reaction/PROACTIVITY_BUDGET_AVAILABLE."""
    decision = decide(_inputs_from_frame(normal_mode_frame), [normal_mode_frame["frame_id"]])

    assert decision.action_type == "aesthetic_reaction"
    assert decision.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE


def test_fixture_baseline_decisions_match_policy(fixture, creative_focus_frame, normal_mode_frame):
    """Baseline decisions in the fixture agree with what decide() returns."""
    baseline = {b["frame_id"]: b for b in fixture["baseline_decisions"]}

    cf_decision = decide(_inputs_from_frame(creative_focus_frame), [creative_focus_frame["frame_id"]])
    assert cf_decision.action_type == baseline["cf-frame-002"]["action_type"]
    assert cf_decision.primary_reason_code.value == baseline["cf-frame-002"]["primary_reason_code"]

    nm_decision = decide(_inputs_from_frame(normal_mode_frame), [normal_mode_frame["frame_id"]])
    assert nm_decision.action_type == baseline["n-frame-002"]["action_type"]
    assert nm_decision.primary_reason_code.value == baseline["n-frame-002"]["primary_reason_code"]
