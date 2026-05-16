"""Tier-B replay bit-identical verification (Phase A.5+1).

Synthesizes a small event log via speak_policy.decide() calls, writes it as
JSONL, feeds it to run_tier_b_replay(), and asserts the recovered SpeakDecision
sequence is bit-identical to the originals across two runs.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from companion_harness import speak_policy
from companion_harness.replay import assert_bit_identical, run_tier_b_replay
from companion_harness.schemas import PolicyInputs, SpeakDecision


def _inputs(
    user_speaking: bool = False,
    eou_probability: float = 0.9,
    user_addressed_agent: bool = True,
) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=user_speaking,
        eou_probability=eou_probability,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=user_addressed_agent,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


def _write_event_log(path: Path, decisions: list[SpeakDecision]) -> None:
    """Write policy_decision JSONL events with full inline payload for replay."""
    with path.open("w", encoding="utf-8") as f:
        for i, d in enumerate(decisions):
            evt = {
                "event_id": f"pd-{i:03d}",
                "event_type": "policy_decision",
                "caused_by": d.caused_by,
                "payload_inline": {
                    "action_type": d.action_type,
                    "primary_reason_code": d.primary_reason_code.value,
                    "supporting_reason_codes": [rc.value for rc in d.supporting_reason_codes],
                    "budget_bucket": d.budget_bucket,
                    "response_content_source": d.response_content_source,
                },
            }
            f.write(json.dumps(evt) + "\n")


_TRACE = [
    (_inputs(user_speaking=True), 0.0),
    (_inputs(eou_probability=0.3), 0.0),
    (_inputs(), 0.0),
    (_inputs(user_addressed_agent=False), 0.0),
]


def test_fixture_run_twice_produces_bit_identical_speak_decisions(
    tmp_path: Path,
) -> None:
    """Feed the same inputs twice; assert SpeakDecision sequences are bit-identical."""
    run1 = [
        speak_policy.decide(inp, signal_event_ids=[f"sig-{i:03d}"], p_backchannel=p_bc)
        for i, (inp, p_bc) in enumerate(_TRACE)
    ]
    run2 = [
        speak_policy.decide(inp, signal_event_ids=[f"sig-{i:03d}"], p_backchannel=p_bc)
        for i, (inp, p_bc) in enumerate(_TRACE)
    ]

    log_path = tmp_path / "run1.jsonl"
    _write_event_log(log_path, run1)

    replayed = run_tier_b_replay(log_path)
    assert_bit_identical(run1, replayed)
    assert_bit_identical(run2, replayed)
