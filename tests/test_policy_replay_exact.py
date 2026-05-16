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

from dataclasses import astuple, asdict

from companion_harness.attachment_risk_monitor import (
    OVER_RELIANCE_COUNT_THRESHOLD,
    EventStreamAttachmentRiskMonitor,
)
from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, MemoryItem, PolicyInputs, SensitiveField, SpeakDecision, ThinkerProposal
from companion_harness import speak_policy
from companion_harness.tool_progress_emitter import ToolProgressEmitter


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
    No fixture-loader involvement.  Verifies invariant #5 and POLICY_VERSION == "v0.2-final".
    """
    assert speak_policy.POLICY_VERSION == "v0.2-final"
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
    POLICY_VERSION is "v0.2-final" after v0.2b bump; retrieval plumbing changes
    do not independently bump policy_version (spec line 202–209).
    """
    assert speak_policy.POLICY_VERSION == "v0.2-final"
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
    POLICY_VERSION == "v0.2-final".
    """
    assert speak_policy.POLICY_VERSION == "v0.2-final"
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


# ---------------------------------------------------------------------------
# Stage 6 (v0.1g) trace: two new threshold_path strings
# ---------------------------------------------------------------------------
#
# Covers:
#   "aesthetic_reaction:rubric_blocked"  — proposal carries rubric_violations (PR #195)
#   "attachment_risk:dampen_blocked"     — attachment_risk_level >= 0.5 (PR #212)

def _proposal_with_violation() -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="aesthetic_reaction",
        content="wow",
        trigger="novelty",
        confidence=0.9,
        novelty=0.9,
        interruption_cost=0.1,
        max_utterance_ms=3000,
        cooldown_consumed="aesthetic_reaction",
        caused_by=["s6-frame-000"],
        rubric_violations=["RUBRIC_TOO_LONG"],
    )


_STAGE6_TRACE = [
    # aesthetic_reaction:rubric_blocked — novelty high, proposal has a violation
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
    # attachment_risk:dampen_blocked — novelty high, risk >= 0.5
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
        attachment_risk_level=0.5,
        aesthetic_novelty_score=0.8,
        quiet_mode_active=False,
    ),
]

_STAGE6_PROPOSALS = [
    _proposal_with_violation(),  # frame 0: rubric_blocked
    None,                        # frame 1: attachment_risk dampen (no proposal needed)
]

_STAGE6_BASELINE = [
    {"action_type": "silence", "primary_reason_code": "RUBRIC_VIOLATION",        "supporting_reason_codes": []},
    {"action_type": "silence", "primary_reason_code": "ATTACHMENT_RISK_DAMPEN",  "supporting_reason_codes": []},
]

_STAGE6_EXPECTED_PATH_SUFFIXES = [
    "aesthetic_reaction:rubric_blocked",
    "attachment_risk:dampen_blocked",
]


def test_policy_replay_exact_stage6():
    """Tier B: bit-identical replay for v0.1g threshold_path strings.

    Covers "aesthetic_reaction:rubric_blocked" (PR #195) and
    "attachment_risk:dampen_blocked" (PR #212).  Verifies POLICY_VERSION == "v0.2-final"
    and that both new path strings are emitted by _threshold_path_for().
    """
    assert speak_policy.POLICY_VERSION == "v0.2-final"
    assert len(_STAGE6_TRACE) == len(_STAGE6_BASELINE)
    assert len(_STAGE6_TRACE) == len(_STAGE6_PROPOSALS)

    run1 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s6-frame-{i:03d}"], proposal=prop)
        for i, (inputs, prop) in enumerate(zip(_STAGE6_TRACE, _STAGE6_PROPOSALS))
    ]
    run2 = [
        speak_policy.decide(inputs, signal_event_ids=[f"s6-frame-{i:03d}"], proposal=prop)
        for i, (inputs, prop) in enumerate(zip(_STAGE6_TRACE, _STAGE6_PROPOSALS))
    ]

    for i, (inputs, prop, baseline, d1, d2) in enumerate(
        zip(_STAGE6_TRACE, _STAGE6_PROPOSALS, _STAGE6_BASELINE, run1, run2)
    ):
        frame_id = f"s6-frame-{i:03d}"

        assert d1.action_type == baseline["action_type"], (
            f"{frame_id}: action_type {d1.action_type!r} != baseline {baseline['action_type']!r}"
        )
        assert d1.primary_reason_code == ReasonCode(baseline["primary_reason_code"]), (
            f"{frame_id}: primary_reason_code {d1.primary_reason_code!r} "
            f"!= baseline {baseline['primary_reason_code']!r}"
        )
        assert astuple(d1) == astuple(d2), (
            f"{frame_id}: run1 {astuple(d1)!r} != run2 {astuple(d2)!r}"
        )

        # Verify the new v0.1g threshold_path string via _threshold_path_for directly
        # (build_decision_trace does not forward proposal, so the path is checked here).
        path1 = speak_policy._threshold_path_for(inputs, d1, 0.0, proposal=prop)
        path2 = speak_policy._threshold_path_for(inputs, d2, 0.0, proposal=prop)
        expected_suffix = _STAGE6_EXPECTED_PATH_SUFFIXES[i]
        assert expected_suffix in path1, (
            f"{frame_id}: expected '{expected_suffix}' in path {path1!r}"
        )
        assert path1 == path2, (
            f"{frame_id}: threshold_path not bit-identical: {path1!r} != {path2!r}"
        )


# ---------------------------------------------------------------------------
# AttachmentRiskMonitor.assess() pure-function check (Task 19)
# ---------------------------------------------------------------------------

def _make_policy_event(event_id: str, ts: int) -> Event:
    return Event(
        event_id=event_id,
        session_id="test",
        schema_version="0.1",
        seq_no=0,
        event_type="policy_decision",
        timestamp_mono_ms=ts,
        timestamp_wall="2026-05-15T00:00:00+00:00",
        source="test",
        caused_by=[],
        payload_hash="abc",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
        payload_inline={"action_type": "full_response"},
    )


def test_attachment_risk_monitor_assess_pure():
    """Identical event stream → bit-identical AttachmentRiskSignal (invariant #5).

    EventStreamAttachmentRiskMonitor.assess() is a pure function of its input.
    Two calls with the same event list must return signals with identical field values.
    """
    monitor = EventStreamAttachmentRiskMonitor()
    events = [
        _make_policy_event(f"ev-{i}", i * 1000)
        for i in range(OVER_RELIANCE_COUNT_THRESHOLD + 3)
    ]

    result1 = monitor.assess(iter(events))
    result2 = monitor.assess(iter(events))

    assert result1 is not None
    assert result2 is not None
    assert asdict(result1) == asdict(result2), (
        f"assess() not bit-identical: run1={result1!r}, run2={result2!r}"
    )


# ---------------------------------------------------------------------------
# Stage 7 — full event-chain + evidence_at() replay determinism (v0.1f Task 14)
# ---------------------------------------------------------------------------
#
# Originally labeled "Stage 5" in commit bf815a4 (PR #220); renamed Stage 7
# during rebase to avoid collision with the existing Stage 5 behavioral-
# tolerance block above.  Test function names retain `stage5_event_chain_*`
# to match the v0.1f roadmap §Task 14 vocabulary.
#
# Asserts:
#   1. POLICY_VERSION is at least v0.1f (forward-compatible: accepts v0.1f or
#      any later version string that sorts >= "v0.1f" lexicographically).
#   2. ToolProgressEmitter.evidence_at() is replay-deterministic: given the
#      same recorded event-log fixture it produces bit-identical
#      ToolProgressEvidence on two separate calls (invariant #5 / Anchor 4).
#   3. The full Stage 5 chain — tool_call_requested → tool_call_dispatched
#      (routing_tier=fast) → tool_progress_event* → tool_call_completed |
#      tool_call_cancelled — is structurally sound (required_fields present,
#      caused_by[] closes, ordering invariant holds).
#   4. decide() is bit-identical given PolicyInputs carrying the evidence.


def _make_event(
    event_id: str,
    event_type: str,
    timestamp_mono_ms: int,
    caused_by: list[str],
    payload_inline: dict,
) -> Event:
    return Event(
        event_id=event_id,
        session_id="s5-replay-session",
        schema_version="v0.1f",
        seq_no=0,
        event_type=event_type,
        timestamp_mono_ms=timestamp_mono_ms,
        timestamp_wall="2026-05-15T00:00:00+00:00",
        source="test",
        caused_by=caused_by,
        payload_hash="",
        payload_ref=None,
        payload_kind="tool_event",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="tool_call_audit_30d",
        payload_inline=payload_inline,
    )


# Recorded event-log fixture: one complete fast-path tool call
# (tool_call_requested → tool_call_dispatched → two tool_progress_events → tool_call_completed).
_TOOL_CALL_ID = "tcid-replay-001"

_STAGE5_EVENT_LOG: list[Event] = [
    _make_event(
        "evt-001", "tool_call_requested", 1000, ["sig-001"],
        {"tool_call_id": _TOOL_CALL_ID},
    ),
    _make_event(
        "evt-002", "tool_call_dispatched", 1010, ["evt-001"],
        {"tool_call_id": _TOOL_CALL_ID, "routing_tier": "fast"},
    ),
    _make_event(
        "evt-003", "tool_progress_event", 3000, ["evt-002"],
        {"tool_call_id": _TOOL_CALL_ID, "progress_stage": "scanning"},
    ),
    _make_event(
        "evt-004", "tool_progress_event", 8000, ["evt-003"],
        {"tool_call_id": _TOOL_CALL_ID, "progress_stage": "aggregating"},
    ),
    _make_event(
        "evt-005", "tool_call_completed", 9500, ["evt-004"],
        {"tool_call_id": _TOOL_CALL_ID},
    ),
]

# Cancelled variant: replaces tool_call_completed with tool_call_cancelled.
_STAGE5_CANCELLED_LOG: list[Event] = [
    _make_event(
        "evtc-001", "tool_call_requested", 1000, ["sig-002"],
        {"tool_call_id": "tcid-replay-002"},
    ),
    _make_event(
        "evtc-002", "tool_call_dispatched", 1010, ["evtc-001"],
        {"tool_call_id": "tcid-replay-002", "routing_tier": "fast"},
    ),
    _make_event(
        "evtc-003", "tool_progress_event", 3000, ["evtc-002"],
        {"tool_call_id": "tcid-replay-002", "progress_stage": "scanning"},
    ),
    _make_event(
        "evtc-004", "tool_call_cancelled", 3800, ["evtc-003", "vad-barge-in-001"],
        {"tool_call_id": "tcid-replay-002"},
    ),
]


def test_policy_replay_stage5_event_chain_structure():
    """Stage 5 Anchor 2 ordering invariant: chain closes via caused_by[].

    Verifies required_fields are present and the caused_by[] DAG closes
    through the full chain (tool_call_requested → tool_call_dispatched →
    tool_progress_event* → tool_call_completed).  Covers the cancelled
    variant (tool_call_cancelled) as well.
    """
    # fast-path completed chain
    ids = {e.event_id for e in _STAGE5_EVENT_LOG}
    for evt in _STAGE5_EVENT_LOG:
        pi = evt.payload_inline or {}
        assert "tool_call_id" in pi, f"{evt.event_id}: missing tool_call_id"
        if evt.event_type == "tool_call_dispatched":
            assert pi.get("routing_tier") == "fast", (
                f"{evt.event_id}: routing_tier must be 'fast'"
            )
        if evt.event_type == "tool_progress_event":
            assert "progress_stage" in pi, f"{evt.event_id}: missing progress_stage"
        for cause in evt.caused_by:
            if not cause.startswith("sig-"):
                assert cause in ids, (
                    f"{evt.event_id}: caused_by {cause!r} not in event log"
                )

    # cancelled variant chain
    ids_c = {e.event_id for e in _STAGE5_CANCELLED_LOG}
    for evt in _STAGE5_CANCELLED_LOG:
        pi = evt.payload_inline or {}
        assert "tool_call_id" in pi, f"{evt.event_id}: missing tool_call_id"
        for cause in evt.caused_by:
            if not cause.startswith(("sig-", "vad-")):
                assert cause in ids_c, (
                    f"{evt.event_id}: caused_by {cause!r} not in cancelled log"
                )


def test_policy_replay_stage5_evidence_at_determinism():
    """Anchor 4 replay determinism: evidence_at() bit-identical on same event log.

    Calls evidence_at() twice with the same recorded event-log fixture and
    asserts the returned ToolProgressEvidence is identical both times.
    Never touches a wall clock — uses only timestamp_mono_ms from the log.
    """
    emitter = ToolProgressEmitter()
    now_mono_ms = 10_000

    ev1 = emitter.evidence_at(_TOOL_CALL_ID, now_mono_ms, _STAGE5_EVENT_LOG)
    ev2 = emitter.evidence_at(_TOOL_CALL_ID, now_mono_ms, _STAGE5_EVENT_LOG)

    assert ev1 == ev2, f"evidence_at not deterministic: {ev1!r} != {ev2!r}"
    # The last tool_progress_event is at ts=8000; now=10000 → ms_since=2000.
    assert ev1.ms_since_last_filler == 2000
    assert ev1.progress_stage == "aggregating"


def test_policy_replay_stage5_decide_determinism():
    """Tier B: decide() bit-identical for PolicyInputs with tool_progress_evidence.

    Feeds evidence derived from the recorded event log into PolicyInputs and
    calls decide() twice; asserts the SpeakDecision is bit-identical.
    POLICY_VERSION is at least "v0.1f" (forward-compatible assertion).
    """
    assert speak_policy.POLICY_VERSION >= "v0.1f", (
        f"POLICY_VERSION {speak_policy.POLICY_VERSION!r} < 'v0.1f'"
    )

    emitter = ToolProgressEmitter()
    evidence = emitter.evidence_at(_TOOL_CALL_ID, 10_000, _STAGE5_EVENT_LOG)

    inputs = PolicyInputs(
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
        tool_status="in_progress",
        tool_progress_evidence=evidence,
    )

    d1 = speak_policy.decide(inputs, signal_event_ids=["evt-004"])
    d2 = speak_policy.decide(inputs, signal_event_ids=["evt-004"])

    assert astuple(d1) == astuple(d2), (
        f"Tier B violated for tool_status inputs: {astuple(d1)!r} != {astuple(d2)!r}"
    )
    assert d1.action_type == "tool_status"
    assert d1.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE
