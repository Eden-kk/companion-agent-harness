"""Tests for the LONG_RESPONSE_GATED policy branch (Option C Stage 2).

Success criterion: speak_policy.decide() returns LONG_RESPONSE_GATED when
len(user_transcript) >= 25, user_addressed_agent is True, and quiet_mode_active
is False — and silences or falls through on all other combinations.
"""

from __future__ import annotations

import pytest

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness.speak_policy import LONG_RESPONSE_GATE_CHARS, decide


def _base_inputs(**overrides) -> PolicyInputs:
    defaults = dict(
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
        quiet_mode_active=False,
        user_transcript="x" * LONG_RESPONSE_GATE_CHARS,
    )
    defaults.update(overrides)
    return PolicyInputs(**defaults)


def test_long_response_gated_fires():
    inputs = _base_inputs()
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code == ReasonCode.LONG_RESPONSE_GATED
    assert decision.action_type == "full_response"
    assert ReasonCode.EOU_CONFIRMED in decision.supporting_reason_codes


def test_short_transcript_does_not_trigger():
    inputs = _base_inputs(user_transcript="x" * (LONG_RESPONSE_GATE_CHARS - 1))
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code != ReasonCode.LONG_RESPONSE_GATED


def test_empty_transcript_does_not_trigger():
    inputs = _base_inputs(user_transcript="")
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code != ReasonCode.LONG_RESPONSE_GATED


def test_quiet_mode_suppresses_gate():
    inputs = _base_inputs(quiet_mode_active=True)
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code != ReasonCode.LONG_RESPONSE_GATED


def test_none_user_addressed_agent_does_not_trigger():
    """None means unknown — must NOT trigger the gate."""
    inputs = _base_inputs(user_addressed_agent=None)
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code != ReasonCode.LONG_RESPONSE_GATED


def test_false_user_addressed_agent_does_not_trigger():
    inputs = _base_inputs(user_addressed_agent=False)
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code != ReasonCode.LONG_RESPONSE_GATED


def test_gate_outranks_backchannel():
    """LONG_RESPONSE_GATED must fire even when p_backchannel is above threshold."""
    inputs = _base_inputs()
    decision = decide(inputs, ["sig-1"], p_backchannel=0.9)
    assert decision.primary_reason_code == ReasonCode.LONG_RESPONSE_GATED


def test_exact_threshold_triggers():
    inputs = _base_inputs(user_transcript="x" * LONG_RESPONSE_GATE_CHARS)
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code == ReasonCode.LONG_RESPONSE_GATED


@pytest.mark.parametrize("length", [25, 50, 100])
def test_various_lengths_above_threshold(length: int) -> None:
    inputs = _base_inputs(user_transcript="w" * length)
    decision = decide(inputs, ["sig-1"])
    assert decision.primary_reason_code == ReasonCode.LONG_RESPONSE_GATED
