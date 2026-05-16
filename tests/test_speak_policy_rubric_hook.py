"""SpeakPolicy rubric hook tests (v0.1g Task 5).

Success criterion: proposal.rubric_violations non-empty → silence + RUBRIC_VIOLATION;
empty violations → aesthetic_reaction permitted; all 8 violation IDs block.
"""

import pytest

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs, ThinkerProposal
from companion_harness.speak_policy import decide


def _aesthetic_inputs() -> PolicyInputs:
    return PolicyInputs(
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
        aesthetic_novelty_score=0.6,
    )


def _proposal(violations: list) -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="aesthetic_reaction",
        content="what a view",
        trigger="scene_change",
        confidence=0.8,
        novelty=0.7,
        interruption_cost=0.1,
        max_utterance_ms=3000,
        cooldown_consumed="aesthetic_reaction",
        caused_by=["sig-1"],
        rubric_violations=violations,
    )


def test_rubric_violation_returns_silence():
    result = decide(_aesthetic_inputs(), ["sig-1"], proposal=_proposal(["RUBRIC_TOO_LONG"]))
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.RUBRIC_VIOLATION


def test_rubric_pass_does_not_block():
    result = decide(_aesthetic_inputs(), ["sig-1"], proposal=_proposal([]))
    assert result.action_type == "aesthetic_reaction"
    assert result.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE


@pytest.mark.parametrize("violation_id", [
    "RUBRIC_TOO_LONG",
    "RUBRIC_UNGROUNDED",
    "RUBRIC_POSSESSIVE",
    "RUBRIC_DIAGNOSTIC",
    "RUBRIC_FLATTERING",
    "RUBRIC_FABRICATED_MEMORY",
    "RUBRIC_ABSENT_SENSORY_CHANNEL",
    "RUBRIC_IDENTITY_ONLY",
])
def test_all_8_violation_ids_block(violation_id):
    result = decide(_aesthetic_inputs(), ["sig-1"], proposal=_proposal([violation_id]))
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.RUBRIC_VIOLATION
