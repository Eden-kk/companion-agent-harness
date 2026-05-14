"""Stage 1 — 10 min scripted long utterances with fillers produce <1 false interruption.

See docs/architecture-v0.1.md §Part 6 Stage 1 and §Part 8 v0.1a acceptance gate
(false_interruption_count_per_10_min < 1).
Fixture: false_interruption_001 — 200 filler/mid-utterance signals sampled every ~3s
across a 600s (10-minute) window.  A "false interruption" is a full_response decision
emitted while the user is still speaking (user_speaking=True or eou_probability below
the policy threshold).
"""

from companion_harness.fixtures.loader import load_fixture
from companion_harness.schemas import PolicyInputs
from companion_harness import speak_policy


def _inputs_from_frame(frame: dict, idx: int) -> PolicyInputs:
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


def test_false_interruption_rate():
    """Feed 200 scripted filler frames through SpeakPolicy; assert <1 false interruption.

    Each frame has user_speaking=True and eou_probability <= 0.45 — the user is
    mid-utterance.  Any full_response decision counts as a false interruption.
    The gate is false_interruption_count_per_10_min < 1 (i.e. == 0 for a single
    10-minute replay run).

    The test can fail: if SpeakPolicy ever returned full_response on a frame where
    user_speaking=True, or relaxed its eou_probability threshold, false_interruptions
    would be > 0 and the assertion would trip.
    """
    fixture = load_fixture("false_interruption_001")
    assert fixture["case_id"] == "false_interruption_001"
    assert fixture["expected_metrics"]["false_interruption_count_per_10_min"] == "<1"

    signal_trace = fixture["signal_trace"]
    assert len(signal_trace) >= 100, (
        f"fixture too short ({len(signal_trace)} frames) to compute a meaningful rate"
    )

    # Verify the fixture actually covers 10 minutes of simulated time
    duration_ms = signal_trace[-1]["t_ms"] - signal_trace[0]["t_ms"]
    assert duration_ms >= 599_000, (
        f"fixture spans only {duration_ms / 1000:.1f}s; need >= 600s for the per-10-min gate"
    )

    false_interruptions: list[int] = []

    for idx, frame in enumerate(signal_trace):
        inputs = _inputs_from_frame(frame, idx)
        decision = speak_policy.decide(inputs, signal_event_ids=[f"filler-signal-{idx:04d}"])

        if decision.action_type == "full_response":
            false_interruptions.append(idx)

    # Rate gate: < 1 false interruption per 10 minutes
    count = len(false_interruptions)
    assert count < 1, (
        f"false_interruption_count_per_10_min = {count} >= gate of 1\n"
        f"  False interruptions at frame indices: {false_interruptions}\n"
        f"  Fixture frames: {len(signal_trace)}, window: {duration_ms / 1000:.0f}s"
    )
