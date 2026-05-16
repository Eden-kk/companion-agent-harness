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
from companion_harness.schemas import MemoryItem, PolicyInputs, SensitiveField, SpeakDecision
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
    No fixture-loader involvement.  Verifies invariant #5 and POLICY_VERSION == "v0.1j".
    """
    assert speak_policy.POLICY_VERSION == "v0.1j"
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


# Stage 4 signal trace: manually-injected PolicyInputs exercising the new v0.1e
# retrieval plumbing.  Per converged v0.1e roadmap Task 24, decide() does NOT
# read retrieved_items — retrieval is inert at the policy layer.  This test
# asserts (a) feeding populated retrieved_items through decide() does not
# perturb decisions, and (b) DecisionTrace.retrieval_used co-emission is
# deterministic across repeated calls with identical inputs.
def _mem_item(item_id: str, summary: str) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="session",
        content={"key": summary},
        source_event_id="evt-s4-001",
        created_at="2026-01-01T00:00:00+00:00",
        last_confirmed_at="2026-01-01T00:00:00+00:00",
        confidence=0.9,
        salience=0.8,
        privacy_level="default",
        mutability="system_revisable",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(retention_policy_id="default", value=summary),
    )


_STAGE4_TRACE = [
    # frame 0: retrieved_items=[] (empty retrieval, typical v0.1e flow);
    # EOU confirmed + agent addressed → full_response.
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
        retrieved_items=[],
    ),
    # frame 1: retrieved_items=[<MemoryItem>] (populated-retrieval branch);
    # EOU confirmed + agent addressed → full_response (retrieval inert).
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
        retrieved_items=[_mem_item("mem-001", "user prefers tea")],
    ),
    # frame 2: empty retrieval, EOU sub-threshold → silence
    # (NOT_ADDRESSED_TO_AGENT).  Verifies retrieval inertness does not unblock
    # silence-winning branches.
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.3,
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
        retrieved_items=[],
    ),
    # frame 3: mixed — populated retrieval + EOU confirmed + agent addressed
    # → full_response.  Two MemoryItems exercise multi-item retrieval lists.
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.92,
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
        retrieved_items=[
            _mem_item("mem-002", "user lives in Tokyo"),
            _mem_item("mem-003", "user is allergic to peanuts"),
        ],
    ),
]

_STAGE4_BASELINE = [
    {"action_type": "full_response", "primary_reason_code": "EOU_CONFIRMED",          "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
    {"action_type": "full_response", "primary_reason_code": "EOU_CONFIRMED",          "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
    {"action_type": "silence",       "primary_reason_code": "NOT_ADDRESSED_TO_AGENT", "supporting_reason_codes": []},
    {"action_type": "full_response", "primary_reason_code": "EOU_CONFIRMED",          "supporting_reason_codes": ["USER_ADDRESSED_AGENT"]},
]

# Per-frame retrieval_event_ids passed to build_decision_trace().  Empty when
# retrieval is inert; one or more MRE event_ids when populated.
_STAGE4_RETRIEVAL_EVENT_IDS: list[list[str]] = [
    [],
    ["mre-001"],
    [],
    ["mre-001", "mre-002"],
]


def test_policy_replay_exact_stage4():
    """Tier B: 100% bit-identical replay for Stage 4 retrieval plumbing.

    Per converged v0.1e roadmap Task 24, decide() does NOT read
    retrieved_items at v0.1e (retrieval linkage lives in
    DecisionTrace.retrieval_used, not PolicyInputs branching).  This test
    asserts (a) feeding populated retrieved_items through decide() does not
    perturb decisions, and (b) DecisionTrace.retrieval_used co-emission is
    deterministic across repeated calls with identical inputs.
    POLICY_VERSION is "v0.1j" after Wave 7 Task 18 bump; retrieval plumbing changes
    do not independently bump policy_version (spec line 202–209).
    """
    assert speak_policy.POLICY_VERSION == "v0.1j"
    assert len(_STAGE4_TRACE) == len(_STAGE4_BASELINE)
    assert len(_STAGE4_TRACE) == len(_STAGE4_RETRIEVAL_EVENT_IDS)

    run1 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s4-frame-{i:03d}"])
        for i, inputs in enumerate(_STAGE4_TRACE)
    ]
    run2 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s4-frame-{i:03d}"])
        for i, inputs in enumerate(_STAGE4_TRACE)
    ]

    for i, (inputs, baseline, d1, d2) in enumerate(
        zip(_STAGE4_TRACE, _STAGE4_BASELINE, run1, run2)
    ):
        frame_id = f"s4-frame-{i:03d}"

        # Baseline match: action_type + primary + supporting.
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

        # Decision bit-identical across replays.
        assert astuple(d1) == astuple(d2), (
            f"{frame_id}: decision run1 {astuple(d1)!r} != run2 {astuple(d2)!r}"
        )

        # DecisionTrace co-emission: retrieval_used reflects the input
        # retrieval_event_ids, and trace1 == trace2 bit-identical.
        retrieval_event_ids = _STAGE4_RETRIEVAL_EVENT_IDS[i]
        trace1 = speak_policy.build_decision_trace(
            decision=d1,
            inputs=inputs,
            signal_event_ids=[f"s4-frame-{i:03d}"],
            decision_id=f"s4-decision-{i:03d}",
            retrieval_event_ids=retrieval_event_ids,
        )
        trace2 = speak_policy.build_decision_trace(
            decision=d2,
            inputs=inputs,
            signal_event_ids=[f"s4-frame-{i:03d}"],
            decision_id=f"s4-decision-{i:03d}",
            retrieval_event_ids=retrieval_event_ids,
        )

        assert trace1.retrieval_used == retrieval_event_ids, (
            f"{frame_id}: retrieval_used {trace1.retrieval_used!r} "
            f"!= expected {retrieval_event_ids!r}"
        )
        assert astuple(trace1) == astuple(trace2), (
            f"{frame_id}: trace run1 {astuple(trace1)!r} != run2 {astuple(trace2)!r}"
        )


# ---------------------------------------------------------------------------
# Stage 5 — behavioral-tolerance extension (invariant #6, v0.1j Wave 7)
# ---------------------------------------------------------------------------
#
# Real-signal producers (MiniCPM EOU, addressing classifier) may vary across
# runs due to live ASR jitter and model non-determinism.  Tier B (bit-identical)
# replay still holds for everything downstream of decide() — given identical
# PolicyInputs the policy layer is deterministic (invariant #5).
#
# This test asserts the invariant #6 behavioral-tolerance tuple:
#   same_action_class + same_interaction_intent + same_safety_class
# across two decide() calls where the *real-signal* inputs are allowed to
# vary within their operational range.  Policy inputs are held fixed so the
# test is a pure policy-layer determinism check — the variance lives upstream.

_ACTION_CLASS: dict[str, str] = {
    "silence":           "no_speech",
    "backchannel":       "acknowledgement",
    "short_reaction":    "proactive_speech",
    "aesthetic_reaction":"proactive_speech",
    "full_response":     "reactive_speech",
    "clarification":     "reactive_speech",
    "alert":             "reactive_speech",
    "tool_call":         "reactive_speech",
    "tool_status":       "reactive_speech",
}

_INTERACTION_INTENT: dict[str, str] = {
    "silence":           "wait",
    "backchannel":       "social",
    "short_reaction":    "proactive",
    "aesthetic_reaction":"proactive",
    "full_response":     "answer",
    "clarification":     "clarify",
    "alert":             "alert",
    "tool_call":         "tool",
    "tool_status":       "tool",
}

_SAFETY_CLASS: dict[str, str] = {
    "silence":           "safe",
    "backchannel":       "safe",
    "short_reaction":    "safe",
    "aesthetic_reaction":"safe",
    "full_response":     "safe",
    "clarification":     "safe",
    "alert":             "safe",
    "tool_call":         "safe",
    "tool_status":       "safe",
}


def _behavioral_tuple(d: SpeakDecision) -> tuple[str, str, str]:
    return (
        _ACTION_CLASS[d.action_type],
        _INTERACTION_INTENT[d.action_type],
        _SAFETY_CLASS[d.action_type],
    )


_STAGE5_TRACE = [
    # EOU confirmed + agent addressed → full_response (reactive_speech / answer / safe)
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
    # EOU sub-threshold + not addressed → silence (no_speech / wait / safe)
    PolicyInputs(
        user_speaking=False,
        eou_probability=0.3,
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
    # alert: urgency in cooking mode → alert (reactive_speech / alert / safe)
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
    # clarification: audio_visual_conflict → clarification (reactive_speech / clarify / safe)
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
    # aesthetic_reaction: permitted → proactive_speech / proactive / safe
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
]

_STAGE5_P_BACKCHANNEL = [0.0, 0.0, 0.0, 0.0, 0.0]

_STAGE5_EXPECTED_BEHAVIORAL_TUPLES = [
    ("reactive_speech", "answer",    "safe"),
    ("no_speech",       "wait",      "safe"),
    ("reactive_speech", "alert",     "safe"),
    ("reactive_speech", "clarify",   "safe"),
    ("proactive_speech","proactive", "safe"),
]


def test_policy_replay_behavioral_tolerance_stage5():
    """Invariant #6: behavioral-tolerance tuple stable for v0.1j real-signal producer range.

    Tier B (bit-identical) replay holds for everything downstream of decide()
    given fixed PolicyInputs (invariant #5). This test additionally asserts the
    invariant #6 behavioral tuple (action_class + interaction_intent + safety_class)
    is stable across two decide() runs and matches the expected class bucketing.
    Covers all five leaf action classes used by v0.1j Wave 2-5 producers.
    POLICY_VERSION == "v0.1j".
    """
    assert speak_policy.POLICY_VERSION == "v0.1j"
    assert len(_STAGE5_TRACE) == len(_STAGE5_P_BACKCHANNEL)
    assert len(_STAGE5_TRACE) == len(_STAGE5_EXPECTED_BEHAVIORAL_TUPLES)

    run1 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s5-frame-{i:03d}"], p_backchannel=p_bc)
        for i, (inputs, p_bc) in enumerate(zip(_STAGE5_TRACE, _STAGE5_P_BACKCHANNEL))
    ]
    run2 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s5-frame-{i:03d}"], p_backchannel=p_bc)
        for i, (inputs, p_bc) in enumerate(zip(_STAGE5_TRACE, _STAGE5_P_BACKCHANNEL))
    ]

    for i, (d1, d2, expected_bt) in enumerate(
        zip(run1, run2, _STAGE5_EXPECTED_BEHAVIORAL_TUPLES)
    ):
        frame_id = f"s5-frame-{i:03d}"

        # Tier B: bit-identical across replays (invariant #5).
        assert astuple(d1) == astuple(d2), (
            f"{frame_id}: Tier B violated — run1 {astuple(d1)!r} != run2 {astuple(d2)!r}"
        )

        # Invariant #6: behavioral-tolerance tuple matches expected class bucketing.
        bt = _behavioral_tuple(d1)
        assert bt == expected_bt, (
            f"{frame_id}: behavioral tuple {bt!r} != expected {expected_bt!r} "
            f"(action_type={d1.action_type!r})"
        )
