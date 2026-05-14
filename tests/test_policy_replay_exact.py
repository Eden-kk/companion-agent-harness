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
