"""Stage 0 Tier B — bit-identical policy replay given recorded signals.

See docs/architecture-v0.1.md §Part 6 Stage 0 and §Part 8 v0.1a acceptance gate
(policy_replay_match_rate = 100%).

Methodology (spec §Part 6 Stage 0):
  input: recorded signal trace (policy_replay_001)
  expected: identical action_type, reason_code per frame
  boundary frame: eou_probability=0.50 (equality → silence; gate 3 is <= 0.5);
    eou_probability=0.45 is sub-threshold (below the boundary, not a boundary value).

Two checks, both required for a non-vacuous test:
  1. Baseline match — each frame's SpeakDecision matches the recorded baseline
     decision encoded in the fixture.  Would catch a regression that changes
     policy logic (e.g. threshold shift, wrong reason code).
  2. Run-twice identity — the trace is replayed a second time; every decision
     must be bit-identical to the first run.  Would catch any non-determinism
     introduced into decide() (wall-clock reads, random(), dict-order dependence).
"""

from dataclasses import astuple

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs, SpeakDecision
from companion_harness import speak_policy


def _inputs_from_frame(frame: dict) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=frame["user_speaking"],
        eou_probability=frame["eou_probability"],
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=frame["user_addressed_agent"],
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode=frame["social_mode"],
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


def _run_trace(signal_trace: list[dict]) -> list[SpeakDecision]:
    return [
        speak_policy.decide(
            _inputs_from_frame(frame),
            signal_event_ids=[frame["frame_id"]],
            p_backchannel=frame.get("p_backchannel", 0.0),
        )
        for frame in signal_trace
    ]


def test_policy_replay_exact():
    """Tier B: 100% bit-identical replay on recorded signal traces.

    Uses fixture policy_replay_001 — 8 frames spanning all 6 decision branches,
    including the v0.1b backchannel branch (frame-007, manually injected p_backchannel=0.85).
    """
    fixture = load_fixture("policy_replay_001")
    assert fixture["case_id"] == "policy_replay_001"
    assert fixture["expected_metrics"]["policy_replay_match_rate"] == "100%"

    signal_trace = fixture["signal_trace"]
    baseline_decisions = fixture["baseline_decisions"]

    assert len(signal_trace) >= 6, (
        f"fixture too small ({len(signal_trace)} frames) to cover all decision branches"
    )

    # Verify the backchannel frame is present and will exercise the new branch.
    backchannel_frames = [f for f in signal_trace if f.get("p_backchannel", 0.0) >= 0.7]
    assert len(backchannel_frames) >= 1, (
        "fixture must contain at least one frame with p_backchannel >= 0.7 to cover the backchannel branch"
    )
    assert len(signal_trace) == len(baseline_decisions)

    run1 = _run_trace(signal_trace)
    run2 = _run_trace(signal_trace)

    matched_baseline = 0
    matched_run2 = 0
    total = len(signal_trace)

    for i, (frame, baseline, d1, d2) in enumerate(
        zip(signal_trace, baseline_decisions, run1, run2)
    ):
        frame_id = frame["frame_id"]

        # Check 1: matches recorded baseline
        assert d1.action_type == baseline["action_type"], (
            f"frame {frame_id}: action_type {d1.action_type!r} != baseline {baseline['action_type']!r}"
        )
        assert d1.primary_reason_code == ReasonCode(baseline["primary_reason_code"]), (
            f"frame {frame_id}: primary_reason_code {d1.primary_reason_code!r} "
            f"!= baseline {baseline['primary_reason_code']!r}"
        )
        expected_supporting = [ReasonCode(c) for c in baseline["supporting_reason_codes"]]
        assert d1.supporting_reason_codes == expected_supporting, (
            f"frame {frame_id}: supporting_reason_codes {d1.supporting_reason_codes!r} "
            f"!= baseline {expected_supporting!r}"
        )
        matched_baseline += 1

        # Check 2: run1 == run2 (bit-identical across replays)
        assert astuple(d1) == astuple(d2), (
            f"frame {frame_id}: run1 {astuple(d1)!r} != run2 {astuple(d2)!r}"
        )
        matched_run2 += 1

    baseline_match_rate = matched_baseline / total
    replay_match_rate = matched_run2 / total

    assert baseline_match_rate == 1.0, (
        f"policy_replay_match_rate (vs baseline) = {baseline_match_rate:.0%}; gate = 100%"
    )
    assert replay_match_rate == 1.0, (
        f"policy_replay_match_rate (run1 vs run2) = {replay_match_rate:.0%}; gate = 100%"
    )


# Stage 2 signal trace: manually-injected PolicyInputs carrying the three new
# v0.1c fields.  No VisionSidecar or DeicticDetector is constructed — this is
# a pure policy-layer determinism test (invariant #5).
_STAGE2_TRACE = [
    # clarification branch: audio_visual_conflict_score > 0.7
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.92,
        assistant_speaking=False,
        scene_change_score=0.3,
        deictic_reference=True,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        audio_visual_conflict_score=0.85,
    ),
    # clarification branch: score exactly at boundary+epsilon
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.88,
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
        audio_visual_conflict_score=0.71,
    ),
    # below conflict threshold with deictic_reference=True → full_response
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.80,
        assistant_speaking=False,
        scene_change_score=0.9,
        deictic_reference=True,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        audio_visual_conflict_score=0.0,
    ),
    # at conflict threshold (0.70 is NOT > 0.7) → full_response; boundary check
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.91,
        assistant_speaking=False,
        scene_change_score=0.5,
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
        audio_visual_conflict_score=0.70,
    ),
]

_STAGE2_BASELINE = [
    {"action_type": "clarification", "primary_reason_code": "AUDIO_VISUAL_CONFLICT", "supporting_reason_codes": []},
    {"action_type": "clarification", "primary_reason_code": "AUDIO_VISUAL_CONFLICT", "supporting_reason_codes": []},
    {"action_type": "full_response",  "primary_reason_code": "EOU_CONFIRMED",         "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
    {"action_type": "full_response",  "primary_reason_code": "EOU_CONFIRMED",         "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
]


def test_policy_replay_exact_stage2():
    """Tier B: 100% bit-identical replay for Stage 2 PolicyInputs fields.

    Manually injects deictic_reference, scene_change_score, and
    audio_visual_conflict_score into SpeakPolicy.decide().  No VisionSidecar
    or DeicticDetector constructed.  Covers the clarification branch
    (audio_visual_conflict_score > 0.7) and boundary determinism for the
    new threshold (invariant #5).
    """
    assert len(_STAGE2_TRACE) == len(_STAGE2_BASELINE)

    run1 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s2-frame-{i:03d}"])
        for i, inputs in enumerate(_STAGE2_TRACE)
    ]
    run2 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s2-frame-{i:03d}"])
        for i, inputs in enumerate(_STAGE2_TRACE)
    ]

    for i, (baseline, d1, d2) in enumerate(zip(_STAGE2_BASELINE, run1, run2)):
        frame_id = f"s2-frame-{i:03d}"

        assert d1.action_type == baseline["action_type"], (
            f"{frame_id}: action_type {d1.action_type!r} != baseline {baseline['action_type']!r}"
        )
        assert d1.primary_reason_code == ReasonCode(baseline["primary_reason_code"]), (
            f"{frame_id}: primary_reason_code {d1.primary_reason_code!r} "
            f"!= baseline {baseline['primary_reason_code']!r}"
        )
        expected_supporting = [ReasonCode(c) for c in baseline["supporting_reason_codes"]]
        assert d1.supporting_reason_codes == expected_supporting, (
            f"{frame_id}: supporting_reason_codes {d1.supporting_reason_codes!r} "
            f"!= baseline {expected_supporting!r}"
        )

        assert astuple(d1) == astuple(d2), (
            f"{frame_id}: run1 {astuple(d1)!r} != run2 {astuple(d2)!r}"
        )


# Stage 3 signal trace: manually-injected PolicyInputs exercising every Stage 3
# branch in speak_policy.decide().  No fixture-loader involvement per roadmap §16.
# task_mode cycling, per-mode alert thresholds, proactivity budget, cooldown_state,
# quiet_mode_active, and aesthetic_novelty_score are all covered.
_STAGE3_TRACE = [
    # alert: cooking mode (low=0.3), urgency_score=0.4 exceeds threshold
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.4,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="cooking",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    ),
    # no alert: cooking mode (low=0.3), urgency_score=0.2 below threshold → full_response
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.2,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="cooking",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    ),
    # alert: normal mode (medium=0.6), urgency_score=0.7 exceeds threshold
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.7,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    ),
    # no alert: normal mode (medium=0.6), urgency_score=0.5 below threshold → full_response
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.5,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    ),
    # alert: crisis_emergency mode (low=0.3), urgency_score=0.4 exceeds threshold
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.4,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="crisis_emergency",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    ),
    # alert: creative_focus mode (high=0.85), urgency_score=0.9 exceeds threshold
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.9,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="creative_focus",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    ),
    # aesthetic_reaction permitted: novelty>0.5, normal mode, no quiet, no cooldown, not addressed
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        aesthetic_novelty_score=0.8,
        quiet_mode_active=False,
    ),
    # aesthetic_reaction blocked by quiet_mode_active=True
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        aesthetic_novelty_score=0.8,
        quiet_mode_active=True,
    ),
    # aesthetic_reaction blocked by mode (cooking disables aesthetic_reaction)
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.2,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="cooking",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        aesthetic_novelty_score=0.8,
        quiet_mode_active=False,
    ),
    # aesthetic_reaction blocked by cooldown_state (rate-limit active)
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={"aesthetic_reaction": 1},
        attachment_risk_level=0.0,
        aesthetic_novelty_score=0.8,
        quiet_mode_active=False,
    ),
    # short_reaction: budget replenished (short_reaction > 0)
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={"short_reaction": 1},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        short_response_appropriate=True,
    ),
    # short_reaction exhausted: budget empty → COOLDOWN_BLOCKED silence
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        short_response_appropriate=True,
    ),
    # backchannel: p_backchannel >= 0.7 (injected via decide() argument)
    PolicyInputs(
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
    ),
    # clarification: audio_visual_conflict_score > 0.7
    PolicyInputs(
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
        audio_visual_conflict_score=0.9,
    ),
    # full_response: EOU confirmed, user addressed agent
    PolicyInputs(
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
    ),
    # silence fallthrough: EOU confirmed, agent not addressed, no proactive trigger
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    ),
]

# p_backchannel per frame (0.0 for all except the backchannel frame at index 12)
_STAGE3_P_BACKCHANNEL = [0.0] * 12 + [0.85] + [0.0] * 3

_STAGE3_BASELINE = [
    {"action_type": "alert",              "primary_reason_code": "ALERT_THRESHOLD_EXCEEDED",  "supporting_reason_codes": []},
    {"action_type": "full_response",      "primary_reason_code": "EOU_CONFIRMED",              "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
    {"action_type": "alert",              "primary_reason_code": "ALERT_THRESHOLD_EXCEEDED",  "supporting_reason_codes": []},
    {"action_type": "full_response",      "primary_reason_code": "EOU_CONFIRMED",              "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
    {"action_type": "alert",              "primary_reason_code": "ALERT_THRESHOLD_EXCEEDED",  "supporting_reason_codes": []},
    {"action_type": "alert",              "primary_reason_code": "ALERT_THRESHOLD_EXCEEDED",  "supporting_reason_codes": []},
    {"action_type": "aesthetic_reaction", "primary_reason_code": "PROACTIVITY_BUDGET_AVAILABLE", "supporting_reason_codes": []},
    {"action_type": "silence",            "primary_reason_code": "QUIET_MODE_BLOCKED",         "supporting_reason_codes": []},
    {"action_type": "silence",            "primary_reason_code": "QUIET_MODE_BLOCKED",         "supporting_reason_codes": []},
    {"action_type": "silence",            "primary_reason_code": "COOLDOWN_BLOCKED",           "supporting_reason_codes": []},
    {"action_type": "short_reaction",     "primary_reason_code": "PROACTIVITY_BUDGET_AVAILABLE", "supporting_reason_codes": []},
    {"action_type": "silence",            "primary_reason_code": "COOLDOWN_BLOCKED",           "supporting_reason_codes": []},
    {"action_type": "backchannel",        "primary_reason_code": "BACKCHANNEL_DETECTED",       "supporting_reason_codes": []},
    {"action_type": "clarification",      "primary_reason_code": "AUDIO_VISUAL_CONFLICT",      "supporting_reason_codes": []},
    {"action_type": "full_response",      "primary_reason_code": "EOU_CONFIRMED",              "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
    {"action_type": "silence",            "primary_reason_code": "NOT_ADDRESSED_TO_AGENT",     "supporting_reason_codes": []},
]


def test_policy_replay_exact_stage3():
    """Tier B: 100% bit-identical replay for Stage 3 PolicyInputs branches.

    Manually injects inputs covering every Stage 3 branch in decide():
    alert (all four modes), aesthetic_reaction (permitted / quiet_mode_blocked /
    mode_blocked / cooldown_blocked), short_reaction (budget replenished /
    exhausted), backchannel, clarification, full_response, and silence fallthrough.
    No fixture-loader involvement.  Verifies invariant #5 and POLICY_VERSION == "v0.1d".
    """
    assert speak_policy.POLICY_VERSION == "v0.1d"
    assert len(_STAGE3_TRACE) == len(_STAGE3_BASELINE)
    assert len(_STAGE3_TRACE) == len(_STAGE3_P_BACKCHANNEL)

    run1 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s3-frame-{i:03d}"], p_backchannel=p_bc)
        for i, (inputs, p_bc) in enumerate(zip(_STAGE3_TRACE, _STAGE3_P_BACKCHANNEL))
    ]
    run2 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s3-frame-{i:03d}"], p_backchannel=p_bc)
        for i, (inputs, p_bc) in enumerate(zip(_STAGE3_TRACE, _STAGE3_P_BACKCHANNEL))
    ]

    for i, (baseline, d1, d2) in enumerate(zip(_STAGE3_BASELINE, run1, run2)):
        frame_id = f"s3-frame-{i:03d}"

        assert d1.action_type == baseline["action_type"], (
            f"{frame_id}: action_type {d1.action_type!r} != baseline {baseline['action_type']!r}"
        )
        assert d1.primary_reason_code == ReasonCode(baseline["primary_reason_code"]), (
            f"{frame_id}: primary_reason_code {d1.primary_reason_code!r} "
            f"!= baseline {baseline['primary_reason_code']!r}"
        )
        expected_supporting = [ReasonCode(c) for c in baseline["supporting_reason_codes"]]
        assert d1.supporting_reason_codes == expected_supporting, (
            f"{frame_id}: supporting_reason_codes {d1.supporting_reason_codes!r} "
            f"!= baseline {expected_supporting!r}"
        )

        assert astuple(d1) == astuple(d2), (
            f"{frame_id}: run1 {astuple(d1)!r} != run2 {astuple(d2)!r}"
        )
