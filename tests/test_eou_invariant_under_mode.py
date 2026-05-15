"""EOU-invariant-under-mode contract test (v0.1d Task 15).

Two-part test against fixture eou_invariant_under_mode_001:

PART 1 — alert-path divergence (the non-vacuous gate):
  urgency_score=0.4 is above the low threshold (0.3) for cooking and
  crisis_emergency but below the medium threshold (0.6) for normal. The three
  modes diverge: cooking + crisis_emergency fire alert; normal does NOT.
  This proves the per-mode alert threshold is genuinely consulted.

PART 2 — EOU invariance (regression-protection):
  urgency_score=0.0 (below all alert thresholds) with eou_probability=0.5
  (exactly at the <= 0.5 boundary). The EOU outcome is IDENTICAL across all
  three modes: silence + NOT_ADDRESSED_TO_AGENT. This is vacuously true today
  (decide() does not read current_task_mode in the EOU branch) but the
  assertion locks the invariant against future drift.

The non-vacuous half is the alert-path divergence (PART 1). The EOU-invariance
half (PART 2) is regression-protection.
"""

import pytest

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness.speak_policy import decide

FIXTURE_ID = "eou_invariant_under_mode_001"


@pytest.fixture(scope="module")
def fixture():
    return load_fixture(FIXTURE_ID)


def _resolution_frames(fixture, part: str) -> dict[str, dict]:
    return {
        f["sub_case"]: f
        for f in fixture["signal_trace"]
        if f.get("part") == part and f.get("resolution_frame")
    }


def _inputs_from_frame(frame: dict) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=frame["user_speaking"],
        eou_probability=frame["eou_probability"],
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=frame["user_addressed_agent"],
        urgency_score=frame["urgency_score"],
        proactivity_budget_remaining=frame["proactivity_budget_remaining"],
        privacy_mode="normal",
        current_task_mode=frame["current_task_mode"],
        social_mode=frame["social_mode"],
        risk_mode="normal",
        cooldown_state=frame["cooldown_state"],
        attachment_risk_level=0.0,
    )


# ---------------------------------------------------------------------------
# PART 1 — alert-path divergence
# ---------------------------------------------------------------------------

def test_alert_divergence_cooking_fires(fixture):
    """PART 1: cooking mode with urgency=0.4 (> low threshold 0.3) → alert."""
    frames = _resolution_frames(fixture, "alert_divergence")
    decision = decide(_inputs_from_frame(frames["cooking"]), ["stub-sig-cooking"])
    assert decision.action_type == "alert"
    assert decision.primary_reason_code == ReasonCode.ALERT_THRESHOLD_EXCEEDED


def test_alert_divergence_crisis_fires(fixture):
    """PART 1: crisis_emergency mode with urgency=0.4 (> low threshold 0.3) → alert."""
    frames = _resolution_frames(fixture, "alert_divergence")
    decision = decide(_inputs_from_frame(frames["crisis_emergency"]), ["stub-sig-crisis"])
    assert decision.action_type == "alert"
    assert decision.primary_reason_code == ReasonCode.ALERT_THRESHOLD_EXCEEDED


def test_alert_divergence_normal_does_not_fire(fixture):
    """PART 1: normal mode with urgency=0.4 (< medium threshold 0.6) → alert does NOT fire."""
    frames = _resolution_frames(fixture, "alert_divergence")
    decision = decide(_inputs_from_frame(frames["normal"]), ["stub-sig-normal"])
    assert decision.action_type != "alert"
    assert decision.primary_reason_code != ReasonCode.ALERT_THRESHOLD_EXCEEDED


def test_alert_divergence_modes_differ(fixture):
    """PART 1: the three modes produce different decisions — threshold is genuinely consulted."""
    frames = _resolution_frames(fixture, "alert_divergence")
    normal_decision = decide(_inputs_from_frame(frames["normal"]), ["stub-sig-normal-2"])
    cooking_decision = decide(_inputs_from_frame(frames["cooking"]), ["stub-sig-cooking-2"])
    crisis_decision = decide(_inputs_from_frame(frames["crisis_emergency"]), ["stub-sig-crisis-2"])
    assert normal_decision.action_type != cooking_decision.action_type
    assert normal_decision.action_type != crisis_decision.action_type
    assert cooking_decision.action_type == crisis_decision.action_type


# ---------------------------------------------------------------------------
# PART 2 — EOU invariance (regression-protection)
# ---------------------------------------------------------------------------

def test_eou_invariance_outcome_identical_across_modes(fixture):
    """PART 2: urgency=0.0, eou=0.5 — all three modes produce silence+NOT_ADDRESSED_TO_AGENT."""
    frames = _resolution_frames(fixture, "eou_invariance")
    decisions = {
        mode: decide(_inputs_from_frame(frame), [f"stub-sig-eou-{mode}"])
        for mode, frame in frames.items()
    }
    for mode, decision in decisions.items():
        assert decision.action_type == "silence", f"mode={mode}: expected silence, got {decision.action_type}"
        assert decision.primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT, (
            f"mode={mode}: expected NOT_ADDRESSED_TO_AGENT, got {decision.primary_reason_code}"
        )


def test_eou_invariance_baseline_decisions_match(fixture):
    """PART 2: fixture baseline_decisions agree with decide() for all eou_invariance frames."""
    baseline = {b["frame_id"]: b for b in fixture["baseline_decisions"] if b["part"] == "eou_invariance"}
    frames = {f["frame_id"]: f for f in fixture["signal_trace"] if f.get("part") == "eou_invariance" and f.get("resolution_frame")}

    for frame_id, frame in frames.items():
        decision = decide(_inputs_from_frame(frame), [f"stub-sig-baseline-{frame_id}"])
        expected = baseline[frame_id]
        assert decision.action_type == expected["action_type"], (
            f"{frame_id}: action_type mismatch — got {decision.action_type}, expected {expected['action_type']}"
        )
        assert decision.primary_reason_code.value == expected["primary_reason_code"], (
            f"{frame_id}: reason_code mismatch — got {decision.primary_reason_code.value}, expected {expected['primary_reason_code']}"
        )
