"""SpeakPolicy — action-type decision from signals; v0.1b output = {silence, backchannel, full_response}.

See docs/architecture-v0.1.md §Part 6 Stage 3 (speak/silence policy, rules-first,
silence wins ties) and §Part 8 (v0.1a restricts the action set to two values;
v0.1b adds the backchannel action type).
Every SpeakDecision must carry a primary_reason_code from ReasonCode — no
free-text reasoning on the policy path (invariant #5 / Stage 0 Tier B replay).
"""

from __future__ import annotations

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs, SpeakDecision

POLICY_VERSION = "v0.1a"

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
        )

    # 6. Deictic grounding below confidence threshold — refuse to invent.
    if inputs.deictic_reference and inputs.grounding_confidence < _GROUNDING_CONFIDENCE_THRESHOLD:
        return _silence(ReasonCode.VISUAL_LOW_CONFIDENCE, caused_by)

    # 7. EOU confirmed.  Respond only when the agent was addressed.
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
        )

    # 8. EOU confirmed but agent not explicitly addressed — silence wins ties.
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
    )
