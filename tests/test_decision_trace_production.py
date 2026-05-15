"""Tests for DecisionTrace production (v0.1e Task 4, issue #34).

Verifies that:
1. build_decision_trace() produces a DecisionTrace linked to the SpeakDecision.
2. The trace is deterministic across two identical calls (invariant #5).
3. counterfactuals["action_selected"] round-trips for multiple action types.
"""

from dataclasses import astuple

from companion_harness.schemas import PolicyInputs
from companion_harness import speak_policy
from companion_harness.speak_policy import build_decision_trace, POLICY_VERSION, CONFIG_VERSION


def _minimal_inputs(**overrides) -> PolicyInputs:
    base = dict(
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
    base.update(overrides)
    return PolicyInputs(**base)


def test_decision_trace_linked_to_speak_decision():
    """build_decision_trace() produces a DecisionTrace with the expected decision_id."""
    inputs = _minimal_inputs()
    signal_ids = ["sig-001"]
    decision = speak_policy.decide(inputs, signal_event_ids=signal_ids)

    trace = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=signal_ids,
        decision_id="decision-001",
    )

    assert trace.decision_id == "decision-001"
    assert trace.primary_reason_code == decision.primary_reason_code
    assert trace.supporting_reason_codes == decision.supporting_reason_codes
    assert trace.policy_version == POLICY_VERSION
    assert trace.config_version == CONFIG_VERSION
    assert trace.model_adapter_versions == {}
    assert trace.retrieval_used == []
    assert trace.redacted_explanation is None
    assert trace.sensitive_explanation_ref is None
    assert signal_ids[0] in trace.signal_event_ids
    assert signal_ids[0] in trace.input_event_ids
    assert isinstance(trace.threshold_path, list)
    assert len(trace.threshold_path) > 0


def test_decision_trace_deterministic():
    """Two calls with identical inputs produce bit-identical DecisionTrace instances."""
    inputs = _minimal_inputs()
    signal_ids = ["sig-det-001"]
    decision = speak_policy.decide(inputs, signal_event_ids=signal_ids)

    trace1 = build_decision_trace(
        decision=decision, inputs=inputs, signal_event_ids=signal_ids, decision_id="det-d001",
    )
    trace2 = build_decision_trace(
        decision=decision, inputs=inputs, signal_event_ids=signal_ids, decision_id="det-d001",
    )

    assert astuple(trace1) == astuple(trace2)


def test_counterfactuals_action_selected_silence():
    """counterfactuals["action_selected"] == "silence" for a silence decision."""
    inputs = _minimal_inputs(user_speaking=True)
    signal_ids = ["sig-sil-001"]
    decision = speak_policy.decide(inputs, signal_event_ids=signal_ids)

    assert decision.action_type == "silence"

    trace = build_decision_trace(
        decision=decision, inputs=inputs, signal_event_ids=signal_ids, decision_id="d-sil",
    )
    assert trace.counterfactuals["action_selected"] == "silence"


def test_counterfactuals_action_selected_full_response():
    """counterfactuals["action_selected"] == "full_response" for a full_response decision."""
    inputs = _minimal_inputs(user_addressed_agent=True, eou_probability=0.9)
    signal_ids = ["sig-fr-001"]
    decision = speak_policy.decide(inputs, signal_event_ids=signal_ids)

    assert decision.action_type == "full_response"

    trace = build_decision_trace(
        decision=decision, inputs=inputs, signal_event_ids=signal_ids, decision_id="d-fr",
    )
    assert trace.counterfactuals["action_selected"] == "full_response"


def test_threshold_path_reflects_branch():
    """threshold_path contains branch-specific markers for different inputs."""
    # social mode block
    inputs_social = _minimal_inputs(social_mode="user_addressing_other")
    decision_social = speak_policy.decide(inputs_social, signal_event_ids=["s1"])
    trace_social = build_decision_trace(
        decision=decision_social, inputs=inputs_social, signal_event_ids=["s1"], decision_id="d-social",
    )
    assert "social_mode_blocked" in trace_social.threshold_path

    # full_response: user_addressed_agent after eou_gate
    inputs_full = _minimal_inputs()
    decision_full = speak_policy.decide(inputs_full, signal_event_ids=["s2"])
    trace_full = build_decision_trace(
        decision=decision_full, inputs=inputs_full, signal_event_ids=["s2"], decision_id="d-full",
    )
    assert "eou_gate:passed" in trace_full.threshold_path
    assert "user_addressed_agent:full_response" in trace_full.threshold_path


def test_trace_stays_deterministic_across_action_types():
    """Determinism check: run each input twice, traces are identical."""
    cases = [
        _minimal_inputs(user_speaking=True),                        # silence
        _minimal_inputs(user_addressed_agent=True),                  # full_response
        _minimal_inputs(urgency_score=0.7, current_task_mode="normal"),  # alert
    ]
    for i, inputs in enumerate(cases):
        signal_ids = [f"det-sig-{i:03d}"]
        p_bc = 0.0
        decision = speak_policy.decide(inputs, signal_event_ids=signal_ids, p_backchannel=p_bc)
        t1 = build_decision_trace(
            decision=decision, inputs=inputs, signal_event_ids=signal_ids,
            decision_id=f"d-det-{i}", p_backchannel=p_bc,
        )
        t2 = build_decision_trace(
            decision=decision, inputs=inputs, signal_event_ids=signal_ids,
            decision_id=f"d-det-{i}", p_backchannel=p_bc,
        )
        assert astuple(t1) == astuple(t2), f"non-deterministic trace for case {i}"
