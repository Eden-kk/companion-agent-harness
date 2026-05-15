"""SpeakPolicy — action-type decision from signals; v0.1b output = {silence, backchannel, full_response}.

See docs/architecture-v0.1.md §Part 6 Stage 3 (speak/silence policy, rules-first,
silence wins ties) and §Part 8 (v0.1a restricts the action set to two values;
v0.1b adds the backchannel action type).
Every SpeakDecision must carry a primary_reason_code from ReasonCode — no
free-text reasoning on the policy path (invariant #5 / Stage 0 Tier B replay).
"""

from __future__ import annotations

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import DecisionTrace, PolicyInputs, ResponseContentSource, SpeakDecision
from companion_harness.speak_policy_config import (
    SPEC_ALERT_THRESHOLD,
    _LEVEL_TO_FLOAT_THRESHOLD,
)

POLICY_VERSION = "v0.1d"
CONFIG_VERSION = "v0.1e"

_RESPONSE_SOURCE_FOR_ACTION: dict[str, ResponseContentSource] = {
    "silence": "no_synthesis",
    "full_response": "foreground_response_proposal",
    "backchannel": "backchannel",
    # filler is content-dependent — set explicitly at construction:
    #   tool evidence present → "filler_with_tool_evidence"
    #   no tool evidence → "filler_without_evidence"
}

_BLOCKING_SOCIAL_MODES = frozenset({
    "user_addressing_other",
    "group_conversation",
    "background_presence",
})

_BACKCHANNEL_THRESHOLD = 0.7
_AUDIO_VISUAL_CONFLICT_THRESHOLD = 0.7
_GROUNDING_CONFIDENCE_THRESHOLD = 0.5


def decide(
    inputs: PolicyInputs,
    signal_event_ids: list[str],
    p_backchannel: float = 0.0,
) -> SpeakDecision:
    """Return a SpeakDecision for the given PolicyInputs.

    Determinism guarantees (invariant #5):
      - No wall-clock reads.
      - No random calls.
      - All comparisons are on the PolicyInputs fields directly.
      - dict iteration (cooldown_state, proactivity_budget_remaining) is never
        used to produce the decision — only keyed lookups and boolean tests.
      - Float comparisons use only the values present in PolicyInputs; no
        floating-point accumulation that could diverge across runs.

    Silence wins ties (invariant #8): every branch that is not an explicit
    full_response condition falls through to silence.
    """
    if not signal_event_ids:
        raise ValueError("signal_event_ids must be non-empty; orphan decisions fail Stage 0")

    caused_by: list[str] = list(signal_event_ids)

    # 1. Hard blocks — silence immediately, no further evaluation.
    if inputs.social_mode in _BLOCKING_SOCIAL_MODES:
        return _silence(ReasonCode.NOT_ADDRESSED_TO_AGENT, caused_by)

    # 1b. Alert-threshold gate — safety-critical; precedes user_speaking so alerts
    # fire even mid-speech (spec Part 6 Stage 3: crisis mode lowers alert threshold;
    # safety alerts override the budget and EOU gate).
    _alert_level = getattr(SPEC_ALERT_THRESHOLD, inputs.current_task_mode, "medium")
    _alert_float = _LEVEL_TO_FLOAT_THRESHOLD[_alert_level]
    if inputs.urgency_score > _alert_float:
        return SpeakDecision(
            action_type="alert",
            primary_reason_code=ReasonCode.ALERT_THRESHOLD_EXCEEDED,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=caused_by,
            budget_bucket="alert",
            allowed_prosody_tags=[],
            max_duration_ms=None,
            response_content_source="foreground_response_proposal",
        )

    # 2. User is still speaking — wait.
    # NOT_ADDRESSED_TO_AGENT covers all "turn not yet handed off" cases (see
    # reason_codes.py docstring and audit note); branches are distinguishable
    # in replay via DecisionTrace.threshold_path. Gap closed — no new member needed.
    if inputs.user_speaking:
        return _silence(ReasonCode.NOT_ADDRESSED_TO_AGENT, caused_by)

    # 3. EOU not confirmed — silence wins ties (invariant #8: tie goes to silence).
    if inputs.eou_probability <= 0.5:
        return _silence(ReasonCode.NOT_ADDRESSED_TO_AGENT, caused_by)

    # 4. EOU confirmed + high backchannel probability — user is just acknowledging.
    if p_backchannel >= _BACKCHANNEL_THRESHOLD:
        return SpeakDecision(
            action_type="backchannel",
            primary_reason_code=ReasonCode.BACKCHANNEL_DETECTED,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=caused_by,
            budget_bucket="backchannel",
            allowed_prosody_tags=[],
            max_duration_ms=None,
            response_content_source="backchannel",
        )

    # 5. Audio-visual conflict exceeds threshold — surface conflict, do not silently agree.
    if inputs.audio_visual_conflict_score > _AUDIO_VISUAL_CONFLICT_THRESHOLD:
        return SpeakDecision(
            action_type="clarification",
            primary_reason_code=ReasonCode.AUDIO_VISUAL_CONFLICT,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=caused_by,
            budget_bucket="clarification",
            allowed_prosody_tags=[],
            max_duration_ms=None,
            response_content_source="foreground_response_proposal",
        )

    # 6. Deictic grounding below confidence threshold — refuse to invent.
    if inputs.deictic_reference and inputs.grounding_confidence < _GROUNDING_CONFIDENCE_THRESHOLD:
        return _silence(ReasonCode.VISUAL_LOW_CONFIDENCE, caused_by)

    # 7. Deictic reference with ambiguous grounding — ask for clarification.
    if inputs.deictic_reference and inputs.deictic_ambiguous:
        return SpeakDecision(
            action_type="clarification",
            primary_reason_code=ReasonCode.DEICTIC_AMBIGUOUS,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=caused_by,
            budget_bucket="clarification",
            allowed_prosody_tags=[],
            max_duration_ms=None,
            response_content_source="foreground_response_proposal",
        )

    # 8. Short reaction: brief proactive reply when the trigger is present and budget allows.
    if inputs.short_response_appropriate:
        if inputs.proactivity_budget_remaining.get("short_reaction", 0) > 0:
            return SpeakDecision(
                action_type="short_reaction",
                primary_reason_code=ReasonCode.PROACTIVITY_BUDGET_AVAILABLE,
                supporting_reason_codes=[],
                redacted_explanation=None,
                caused_by=caused_by,
                budget_bucket="short_reaction",
                allowed_prosody_tags=[],
                max_duration_ms=None,
                response_content_source="foreground_response_proposal",
            )
        return _silence(ReasonCode.COOLDOWN_BLOCKED, caused_by)

    # 9. EOU confirmed.  Respond only when the agent was addressed.
    if inputs.user_addressed_agent:
        return SpeakDecision(
            action_type="full_response",
            primary_reason_code=ReasonCode.EOU_CONFIRMED,
            supporting_reason_codes=[ReasonCode.USER_ADDRESSED_AGENT],
            redacted_explanation=None,
            caused_by=caused_by,
            # budget_bucket selects the per-action latency budget from speak_policy config
            budget_bucket="full_response",
            allowed_prosody_tags=[],
            max_duration_ms=None,
            response_content_source="foreground_response_proposal",
        )

    # 10. Aesthetic reaction — lowest priority, fires only when nothing else fires.
    _AESTHETIC_DISABLED_MODES = frozenset({
        "creative_focus", "sleep_winddown", "group_unaddressed", "cooking",
        "crisis_emergency",
    })
    if inputs.aesthetic_novelty_score > 0.5:
        if inputs.quiet_mode_active or inputs.current_task_mode in _AESTHETIC_DISABLED_MODES:
            return _silence(ReasonCode.QUIET_MODE_BLOCKED, caused_by)
        if inputs.cooldown_state.get("aesthetic_reaction", 0) > 0:
            return _silence(ReasonCode.COOLDOWN_BLOCKED, caused_by)
        return SpeakDecision(
            action_type="aesthetic_reaction",
            primary_reason_code=ReasonCode.PROACTIVITY_BUDGET_AVAILABLE,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=caused_by,
            budget_bucket="aesthetic_reaction",
            allowed_prosody_tags=[],
            max_duration_ms=None,
            response_content_source="foreground_response_proposal",
        )


    # 11. EOU confirmed but agent not explicitly addressed — silence wins ties.
    return _silence(ReasonCode.NOT_ADDRESSED_TO_AGENT, caused_by)


def _silence(reason: ReasonCode, caused_by: list[str]) -> SpeakDecision:
    return SpeakDecision(
        action_type="silence",
        primary_reason_code=reason,
        supporting_reason_codes=[],
        redacted_explanation=None,
        caused_by=caused_by,
        budget_bucket=None,
        allowed_prosody_tags=[],
        max_duration_ms=None,
        response_content_source="no_synthesis",
    )


def _threshold_path_for(inputs: PolicyInputs, decision: SpeakDecision, p_backchannel: float) -> list[str]:
    """Reconstruct the ordered threshold path traversed by decide() for the given inputs.

    Each string names a gate that was evaluated, in evaluation order.  The list
    is deterministic given (inputs, decision, p_backchannel) and contains only
    enum-safe strings (no PII, no free text).
    """
    path: list[str] = []
    if inputs.social_mode in _BLOCKING_SOCIAL_MODES:
        path.append("social_mode_blocked")
        return path
    _alert_level = getattr(SPEC_ALERT_THRESHOLD, inputs.current_task_mode, "medium")
    _alert_float = _LEVEL_TO_FLOAT_THRESHOLD[_alert_level]
    path.append(f"alert_threshold:{inputs.current_task_mode}:{_alert_level}")
    if inputs.urgency_score > _alert_float:
        path.append("alert_threshold:exceeded")
        return path
    if inputs.user_speaking:
        path.append("user_speaking:blocked")
        return path
    path.append("eou_gate")
    if inputs.eou_probability <= 0.5:
        path.append("eou_gate:below_threshold")
        return path
    path.append("eou_gate:passed")
    if p_backchannel >= _BACKCHANNEL_THRESHOLD:
        path.append("backchannel_threshold:exceeded")
        return path
    if inputs.audio_visual_conflict_score > _AUDIO_VISUAL_CONFLICT_THRESHOLD:
        path.append("audio_visual_conflict:exceeded")
        return path
    if inputs.deictic_reference and inputs.grounding_confidence < _GROUNDING_CONFIDENCE_THRESHOLD:
        path.append("grounding_confidence:below_threshold")
        return path
    if inputs.deictic_reference and inputs.deictic_ambiguous:
        path.append("deictic_ambiguous:clarification")
        return path
    if inputs.short_response_appropriate:
        if inputs.proactivity_budget_remaining.get("short_reaction", 0) > 0:
            path.append("short_reaction_budget:available")
        else:
            path.append("short_reaction_budget:exhausted")
        return path
    if inputs.user_addressed_agent:
        path.append("user_addressed_agent:full_response")
        return path
    if inputs.aesthetic_novelty_score > 0.5:
        path.append("aesthetic_novelty:above_threshold")
        _AESTHETIC_DISABLED_MODES = frozenset({
            "creative_focus", "sleep_winddown", "group_unaddressed", "cooking",
            "crisis_emergency",
        })
        if inputs.quiet_mode_active or inputs.current_task_mode in _AESTHETIC_DISABLED_MODES:
            path.append("aesthetic_reaction:mode_blocked")
        elif inputs.cooldown_state.get("aesthetic_reaction", 0) > 0:
            path.append("aesthetic_reaction:cooldown_blocked")
        else:
            path.append("aesthetic_reaction:permitted")
        return path
    path.append("silence:fallthrough")
    return path


def build_decision_trace(
    decision: SpeakDecision,
    inputs: PolicyInputs,
    signal_event_ids: list[str],
    decision_id: str,
    config_version: str = CONFIG_VERSION,
    input_event_ids: list[str] | None = None,
    p_backchannel: float = 0.0,
    retrieval_event_ids: list[str] | None = None,
) -> DecisionTrace:
    """Construct a DecisionTrace linked to a SpeakDecision by shared decision_id.

    Pure function — no I/O, no wall-clock reads.  Safe to call on the replay path.
    input_event_ids defaults to signal_event_ids when not provided.
    """
    return DecisionTrace(
        decision_id=decision_id,
        input_event_ids=list(input_event_ids) if input_event_ids is not None else list(signal_event_ids),
        signal_event_ids=list(signal_event_ids),
        threshold_path=_threshold_path_for(inputs, decision, p_backchannel),
        primary_reason_code=decision.primary_reason_code,
        supporting_reason_codes=list(decision.supporting_reason_codes),
        counterfactuals={"action_selected": decision.action_type},
        redacted_explanation=None,
        sensitive_explanation_ref=None,
        policy_version=POLICY_VERSION,
        config_version=config_version,
        model_adapter_versions={},
        retrieval_used=list(retrieval_event_ids) if retrieval_event_ids else [],
    )
