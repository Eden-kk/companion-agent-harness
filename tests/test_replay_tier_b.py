"""Unit tests for companion_harness.replay (Tier-B replayer)."""

from __future__ import annotations

import json
from dataclasses import astuple
from pathlib import Path

import pytest

from companion_harness import speak_policy
from companion_harness.reason_codes import ReasonCode
from companion_harness.replay import assert_bit_identical, run_tier_b_replay
from companion_harness.schemas import PolicyInputs, SpeakDecision


def _full_response_inputs() -> PolicyInputs:
    return PolicyInputs(
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
    )


def _silence_inputs() -> PolicyInputs:
    return PolicyInputs(
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
    )


def _write_log(path: Path, decisions: list[SpeakDecision]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i, d in enumerate(decisions):
            obj = {
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
            f.write(json.dumps(obj) + "\n")


def test_run_tier_b_replay_reconstructs_decisions_from_event_log(tmp_path: Path) -> None:
    decisions = [
        speak_policy.decide(_full_response_inputs(), signal_event_ids=["sig-001"]),
        speak_policy.decide(_silence_inputs(), signal_event_ids=["sig-002"]),
    ]
    log = tmp_path / "test.jsonl"
    _write_log(log, decisions)

    replayed = run_tier_b_replay(log)

    assert len(replayed) == 2
    assert replayed[0].action_type == "full_response"
    assert replayed[0].primary_reason_code == ReasonCode.EOU_CONFIRMED
    assert replayed[1].action_type == "silence"
    assert replayed[1].primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT


def test_assert_bit_identical_passes_on_same_log(tmp_path: Path) -> None:
    decisions = [
        speak_policy.decide(_full_response_inputs(), signal_event_ids=["sig-001"]),
    ]
    log = tmp_path / "test.jsonl"
    _write_log(log, decisions)

    replayed = run_tier_b_replay(log)
    assert_bit_identical(decisions, replayed)


def test_assert_bit_identical_fails_on_modified_log(tmp_path: Path) -> None:
    d1 = speak_policy.decide(_full_response_inputs(), signal_event_ids=["sig-001"])
    d2 = speak_policy.decide(_silence_inputs(), signal_event_ids=["sig-002"])

    log = tmp_path / "test.jsonl"
    _write_log(log, [d1])

    replayed = run_tier_b_replay(log)
    with pytest.raises(AssertionError):
        assert_bit_identical([d2], replayed)


def test_replay_handles_config_change_events(tmp_path: Path) -> None:
    decision = speak_policy.decide(_full_response_inputs(), signal_event_ids=["sig-001"])
    log = tmp_path / "test.jsonl"

    with log.open("w", encoding="utf-8") as f:
        # config_change event — must be skipped by the replayer
        f.write(json.dumps({
            "event_id": "cc-001",
            "event_type": "config_change",
            "caused_by": [],
            "payload_inline": {
                "key": "backchannel_threshold",
                "previous_value": 0.7,
                "new_value": 0.8,
                "applied_at_ms": 1000,
                "operator_action_event_id": "op-001",
            },
        }) + "\n")
        # policy_decision follows the config_change
        f.write(json.dumps({
            "event_id": "pd-001",
            "event_type": "policy_decision",
            "caused_by": decision.caused_by,
            "payload_inline": {
                "action_type": decision.action_type,
                "primary_reason_code": decision.primary_reason_code.value,
                "supporting_reason_codes": [rc.value for rc in decision.supporting_reason_codes],
                "budget_bucket": decision.budget_bucket,
                "response_content_source": decision.response_content_source,
            },
        }) + "\n")

    replayed = run_tier_b_replay(log)
    assert len(replayed) == 1
    assert_bit_identical([decision], replayed)


def test_replay_handles_threshold_path_changes(tmp_path: Path) -> None:
    """Decisions recorded before and after a threshold change are both replayed."""
    before = speak_policy.decide(_full_response_inputs(), signal_event_ids=["sig-001"])
    after = speak_policy.decide(_silence_inputs(), signal_event_ids=["sig-002"])

    log = tmp_path / "test.jsonl"
    with log.open("w", encoding="utf-8") as f:
        # decision before threshold change
        f.write(json.dumps({
            "event_id": "pd-001",
            "event_type": "policy_decision",
            "caused_by": before.caused_by,
            "payload_inline": {
                "action_type": before.action_type,
                "primary_reason_code": before.primary_reason_code.value,
                "supporting_reason_codes": [rc.value for rc in before.supporting_reason_codes],
                "budget_bucket": before.budget_bucket,
                "response_content_source": before.response_content_source,
            },
        }) + "\n")
        # config_change: threshold updated
        f.write(json.dumps({
            "event_id": "cc-001",
            "event_type": "config_change",
            "caused_by": [],
            "payload_inline": {
                "key": "eou_threshold",
                "previous_value": 0.5,
                "new_value": 0.6,
                "applied_at_ms": 2000,
                "operator_action_event_id": "op-001",
            },
        }) + "\n")
        # decision after threshold change
        f.write(json.dumps({
            "event_id": "pd-002",
            "event_type": "policy_decision",
            "caused_by": after.caused_by,
            "payload_inline": {
                "action_type": after.action_type,
                "primary_reason_code": after.primary_reason_code.value,
                "supporting_reason_codes": [rc.value for rc in after.supporting_reason_codes],
                "budget_bucket": after.budget_bucket,
                "response_content_source": after.response_content_source,
            },
        }) + "\n")

    replayed = run_tier_b_replay(log)
    assert len(replayed) == 2
    assert_bit_identical([before, after], replayed)
