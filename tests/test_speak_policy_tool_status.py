"""Tests for SpeakPolicy tool_status wiring (v0.1f Task 5)."""

from __future__ import annotations

import pytest

from companion_harness import speak_policy
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness.tool_progress import ToolProgressEvidence


def _base_inputs(**overrides) -> PolicyInputs:
    """Minimal PolicyInputs that pass through to the tool_status gate."""
    defaults = dict(
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
        social_mode="solo",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        tool_status="in_progress",
    )
    defaults.update(overrides)
    return PolicyInputs(**defaults)


def _evidence(*, fillers_emitted: int = 0, silence_won: bool = False) -> ToolProgressEvidence:
    return ToolProgressEvidence(
        progress_stage="scanning",
        fillers_emitted_so_far=fillers_emitted,
        ms_since_last_filler=5_000,
        silence_won_already=silence_won,
    )


def test_tool_in_progress_with_evidence_returns_filler():
    inputs = _base_inputs(tool_progress_evidence=_evidence(fillers_emitted=0))
    decision = speak_policy.decide(inputs, signal_event_ids=["sig-001"])
    assert decision.action_type == "tool_status"
    assert decision.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE
    assert decision.response_content_source == "filler_with_tool_evidence"


def test_tool_in_progress_with_filler_budget_exhausted_returns_silence():
    inputs = _base_inputs(tool_progress_evidence=_evidence(fillers_emitted=2))
    decision = speak_policy.decide(inputs, signal_event_ids=["sig-002"])
    assert decision.action_type == "silence"
    assert decision.primary_reason_code == ReasonCode.TOOL_FILLER_BUDGET_EXHAUSTED


def test_tool_in_progress_with_evidence_missing_returns_silence():
    inputs = _base_inputs(tool_progress_evidence=None)
    decision = speak_policy.decide(inputs, signal_event_ids=["sig-003"])
    assert decision.action_type == "silence"
    assert decision.primary_reason_code == ReasonCode.TOOL_PROGRESS_EVIDENCE_MISSING


def test_policy_version_bumped_to_v0_2_final():
    assert speak_policy.POLICY_VERSION == "v0.2-final"
