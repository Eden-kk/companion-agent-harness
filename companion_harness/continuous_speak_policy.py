"""continuous_speak_policy — per-chunk gate for the turn-free continuous companion (PR2).

`decide_chunk()` is the continuous analogue of `speak_policy.decide()`: a pure,
deterministic function from a recorded `PerChunkPolicyInputs` → `SpeakDecision`.
It reuses the `ReasonCode` taxonomy and `SpeakDecision` shape; it does NOT reuse
the per-turn `PolicyInputs` schema or thresholds (those are turn-defined). PR3
wires this into `ContinuousOrchestrator` (replacing the placeholder hook) and
builds the real `PerChunkPolicyInputs` from live signals + emits the DecisionTrace.

Determinism (invariant #5): no wall-clock, no random, scalar/enum fields only,
no dict/set iteration. Given the same recorded `PerChunkPolicyInputs` the output
is bit-identical (Tier-B replay).

Silence wins ties (invariant #8): every branch that is not an explicit speak
condition falls through to silence.
"""

from __future__ import annotations

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PerChunkPolicyInputs, SpeakDecision

POLICY_VERSION = "v0.2-continuous-pr2"

_BLOCKING_SOCIAL_MODES = frozenset({
    "user_addressing_other",
    "group_conversation",
    "background_presence",
})
_BLOCKING_PRIVACY_MODES = frozenset({"sensitive_conversation"})
_BACKCHANNEL_THRESHOLD = 0.7


def _silence(reason: ReasonCode, caused_by: list[str]) -> SpeakDecision:
    return SpeakDecision(
        action_type="silence",
        primary_reason_code=reason,
        supporting_reason_codes=[],
        redacted_explanation=None,
        caused_by=list(caused_by),
        budget_bucket=None,
        allowed_prosody_tags=[],
        max_duration_ms=None,
        response_content_source="no_synthesis",
    )


def decide_chunk(inputs: PerChunkPolicyInputs, *, caused_by_evt_id: str) -> SpeakDecision:
    """Pure, deterministic per-chunk speak/silence decision.

    PR2 implements the minimal real gate; PR3 extends it with the full barge-in
    routing and the remaining signal fields. Keep every comparison on scalar
    fields only — no dict iteration — so Tier-B replay stays bit-identical.
    """
    caused_by = [caused_by_evt_id]

    # 1. Hard blocks — silence immediately (silence wins ties).
    if inputs.social_mode in _BLOCKING_SOCIAL_MODES:
        return _silence(ReasonCode.NOT_ADDRESSED_TO_AGENT, caused_by)
    if inputs.privacy_mode in _BLOCKING_PRIVACY_MODES:
        return _silence(ReasonCode.PRIVACY_MODE_BLOCKED, caused_by)

    # 2. Model is in listen mode this chunk — it is not trying to speak → silence.
    #    (PR3's DecisionTrace.threshold_path will distinguish listen-mode-silence
    #     from social/privacy-block-silence; all map to NOT_ADDRESSED_TO_AGENT today.)
    if inputs.model_is_listen:
        return _silence(ReasonCode.NOT_ADDRESSED_TO_AGENT, caused_by)

    # 3. Model wants to speak, but only if addressed (silence wins ties).
    if not inputs.user_addressed_agent:
        return _silence(ReasonCode.NOT_ADDRESSED_TO_AGENT, caused_by)

    # 4. High backchannel score — user is acknowledging; a backchannel, not a turn.
    if inputs.backchannel_score >= _BACKCHANNEL_THRESHOLD:
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

    # 5. Proactivity budget exhausted — silence.
    if inputs.budget_full_response_remaining <= 0:
        return _silence(ReasonCode.COOLDOWN_BLOCKED, caused_by)

    # 6. Model wants to speak + addressed + budget available → full_response.
    return SpeakDecision(
        action_type="full_response",
        primary_reason_code=ReasonCode.USER_ADDRESSED_AGENT,
        supporting_reason_codes=[],
        redacted_explanation=None,
        caused_by=caused_by,
        budget_bucket="full_response",
        allowed_prosody_tags=[],
        max_duration_ms=None,
        response_content_source="foreground_response_proposal",
    )
