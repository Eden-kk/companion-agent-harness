"""Stage 1 — "what do you think?" type explicit handoff must respond promptly.

See docs/architecture-v0.1.md §Part 8 v0.1a required tests; paired with
test_direct_question_latency to prevent "passes by being sluggish."
"""

import pytest

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness import speak_policy


def _base_inputs(**overrides) -> PolicyInputs:
    defaults = dict(
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
    )
    defaults.update(overrides)
    return PolicyInputs(**defaults)


def test_explicit_turn_handoff():
    """Explicit handoff ('what do you think?') → policy must decide full_response.

    Two sub-checks:
      1. handoff case: user_addressed_agent=True, eou confirmed → full_response
      2. contrast case: user_addressed_agent=False, eou confirmed → silence
         This proves the handoff signal is what drives the decision, not just EOU.
    """
    fixture = load_fixture("explicit_turn_handoff_001")
    assert fixture["case_id"] == "explicit_turn_handoff_001"

    handoff_frame = fixture["signal_trace"][1]
    contrast_frame = fixture["contrast_trace"][0]

    # ── Handoff case: explicit "what do you think?" address ──────────────────
    handoff_inputs = _base_inputs(
        user_speaking=handoff_frame["user_speaking"],
        eou_probability=handoff_frame["eou_probability"],
        user_addressed_agent=handoff_frame["user_addressed_agent"],
    )
    decision = speak_policy.decide(handoff_inputs, signal_event_ids=["eou-signal-001"])

    assert decision.action_type == "full_response", (
        f"explicit handoff must produce full_response; got {decision.action_type!r}"
    )
    assert decision.primary_reason_code == ReasonCode.EOU_CONFIRMED
    assert ReasonCode.USER_ADDRESSED_AGENT in decision.supporting_reason_codes
    assert decision.caused_by == ["eou-signal-001"]

    # ── Contrast case: EOU confirmed, social_mode still user_addressing_agent,
    #    but user_addressed_agent=False → silence.  Proves user_addressed_agent
    #    is the gate-4 discriminator, not social_mode or EOU alone. ────────────
    contrast_inputs = _base_inputs(
        user_speaking=contrast_frame["user_speaking"],
        eou_probability=contrast_frame["eou_probability"],
        user_addressed_agent=contrast_frame["user_addressed_agent"],
    )
    contrast_decision = speak_policy.decide(contrast_inputs, signal_event_ids=["eou-signal-002"])

    assert contrast_decision.action_type == "silence", (
        f"non-handoff EOU must stay silent; got {contrast_decision.action_type!r}"
    )
    assert contrast_decision.primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT
