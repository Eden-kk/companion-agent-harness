"""v0.1g Task 15: proactivity budget contract tests.

Stage 3 carryforward + v0.1g keys:
- aesthetic_reaction proposals consume budget; once exhausted → silence.
- user-reduction commands halve/zero the budget (cross-check with PR #201).

Per OQ-6: proactivity_budget_remaining alphabet is empty-set from v0.1f;
aesthetic_reaction is the only key v0.1g mutates.
"""

from __future__ import annotations

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs, ThinkerProposal
from companion_harness.speak_policy import decide
from companion_harness.user_reduction_commands import apply_user_reduction_command


def _aesthetic_inputs(budget: dict[str, int], cooldown: dict[str, int] | None = None) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining=budget,
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state=cooldown or {},
        attachment_risk_level=0.0,
        aesthetic_novelty_score=0.8,
    )


def _clean_proposal() -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="aesthetic_reaction",
        content="nice light",
        trigger="scene_change",
        confidence=0.8,
        novelty=0.7,
        interruption_cost=0.1,
        max_utterance_ms=3000,
        cooldown_consumed="aesthetic_reaction",
        caused_by=["sig-1"],
        rubric_violations=[],
    )


# ---------------------------------------------------------------------------
# Budget available → action permitted
# ---------------------------------------------------------------------------

def test_aesthetic_reaction_with_budget_available():
    """aesthetic_reaction fires when novelty > 0.5 and no blocking condition."""
    result = decide(_aesthetic_inputs({}), ["sig-1"], proposal=_clean_proposal())
    assert result.action_type == "aesthetic_reaction"
    assert result.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE


# ---------------------------------------------------------------------------
# Cooldown blocks (simulating budget exhaustion via cooldown_state)
# ---------------------------------------------------------------------------

def test_aesthetic_reaction_cooldown_blocks_second():
    """cooldown_state={"aesthetic_reaction": 1} → silence (budget exhausted via cooldown)."""
    result = decide(
        _aesthetic_inputs({}, cooldown={"aesthetic_reaction": 1}),
        ["sig-1"],
        proposal=_clean_proposal(),
    )
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.COOLDOWN_BLOCKED


# ---------------------------------------------------------------------------
# User reduction: less_proactive halves aesthetic_reaction budget
# ---------------------------------------------------------------------------

def test_less_proactive_halves_budget():
    budget = {"aesthetic_reaction": 10}
    result = apply_user_reduction_command("less_proactive", budget)
    assert result["aesthetic_reaction"] == 5


def test_less_proactive_budget_after_halving_still_permits_action():
    """After halving, if remaining > 0, policy still permits aesthetic_reaction."""
    budget_after = apply_user_reduction_command("less_proactive", {"aesthetic_reaction": 4})
    assert budget_after["aesthetic_reaction"] == 2
    # The budget dict itself reflects remaining capacity; policy permits.
    result = decide(_aesthetic_inputs(budget_after), ["sig-1"], proposal=_clean_proposal())
    assert result.action_type == "aesthetic_reaction"


# ---------------------------------------------------------------------------
# User reduction: quiet_mode zeros aesthetic_reaction budget
# ---------------------------------------------------------------------------

def test_quiet_mode_zeros_budget():
    budget = {"aesthetic_reaction": 10}
    result = apply_user_reduction_command("quiet_mode", budget)
    assert result["aesthetic_reaction"] == 0


def test_quiet_mode_policy_blocks_via_quiet_mode_active():
    """quiet_mode_active=True → QUIET_MODE_BLOCKED for aesthetic_reaction."""
    inputs = PolicyInputs(
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
    )
    result = decide(inputs, ["sig-1"], proposal=_clean_proposal())
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.QUIET_MODE_BLOCKED


# ---------------------------------------------------------------------------
# Budget independence: other keys not mutated
# ---------------------------------------------------------------------------

def test_user_reduction_does_not_affect_other_budget_keys():
    budget = {"aesthetic_reaction": 10, "short_reaction": 5}
    result = apply_user_reduction_command("less_proactive", budget)
    assert result["short_reaction"] == 5

    result2 = apply_user_reduction_command("quiet_mode", budget)
    assert result2["short_reaction"] == 5


def test_budget_mutation_does_not_mutate_original():
    budget = {"aesthetic_reaction": 8}
    apply_user_reduction_command("less_proactive", budget)
    assert budget["aesthetic_reaction"] == 8
