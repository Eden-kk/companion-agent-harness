"""Tier-B allowlist schema for manual-test runtime config.

Source of truth for which numeric thresholds are tunable at runtime via the
manual-test dashboard, plus per-key metadata (default / min / max / step /
value_type / one-line description / code-location citation).

See docs/design-config-and-dashboard.md §1 (Tier A/B/C audit) and §5
(Server API → ``TierBSchemaEntry``) for the design rationale and the
authoritative list of 12 Tier-B keys.

This module is import-light: stdlib only. ConfigStore (Task C) and the
HTTP endpoints (Task E) will import :data:`ALLOWLIST` and call
:func:`validate_patch`.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TierBSchemaEntry:
    """Per-key schema entry for a Tier-B tunable parameter.

    Attributes:
        key: Canonical config key, dot-separated (e.g. ``"policy.backchannel_threshold"``).
        code_location: ``"<filename>:<line>"`` citation of the in-code default.
        default: Current code-time default value.
        min: Slider lower bound (inclusive).
        max: Slider upper bound (inclusive).
        step: Slider increment.
        value_type: Either ``float`` or ``int``; used for type-mismatch rejection.
        description: Short, dashboard-displayable one-liner explaining the knob.
    """

    key: str
    code_location: str
    default: float | int
    min: float | int
    max: float | int
    step: float | int
    value_type: type
    description: str


# The 12-key Tier-B allowlist. See docs/design-config-and-dashboard.md §1.
ALLOWLIST: dict[str, TierBSchemaEntry] = {
    # --- Policy thresholds (speak_policy.py) ---
    "policy.backchannel_threshold": TierBSchemaEntry(
        key="policy.backchannel_threshold",
        code_location="speak_policy.py:28",
        default=0.7,
        min=0.40,
        max=0.95,
        step=0.01,
        value_type=float,
        description="Above this p_backchannel, EOU → backchannel action",
    ),
    "policy.audio_visual_conflict_threshold": TierBSchemaEntry(
        key="policy.audio_visual_conflict_threshold",
        code_location="speak_policy.py:29",
        default=0.7,
        min=0.40,
        max=0.95,
        step=0.01,
        value_type=float,
        description="Above this audio_visual_conflict_score, action → clarification",
    ),
    "policy.grounding_confidence_threshold": TierBSchemaEntry(
        key="policy.grounding_confidence_threshold",
        code_location="speak_policy.py:30",
        default=0.5,
        min=0.20,
        max=0.90,
        step=0.01,
        value_type=float,
        description=(
            "Below this grounding_confidence on a deictic reference, "
            "response is suppressed"
        ),
    ),
    # --- VAD detector (turn_detector_vad.py) ---
    "detectors.vad.speech_threshold": TierBSchemaEntry(
        key="detectors.vad.speech_threshold",
        code_location="turn_detector_vad.py:26",
        default=0.5,
        min=0.20,
        max=0.90,
        step=0.01,
        value_type=float,
        description="Silero VAD per-frame p_speech gate",
    ),
    "detectors.vad.silence_onset_ms": TierBSchemaEntry(
        key="detectors.vad.silence_onset_ms",
        code_location="turn_detector_vad.py:27",
        default=300,
        min=150,
        max=800,
        step=10,
        value_type=int,
        description="Consecutive silence ms required to fire EOU",
    ),
    # --- Smart-turn detector (turn_detector_smart.py) ---
    "detectors.smart_turn.silence_onset_ms": TierBSchemaEntry(
        key="detectors.smart_turn.silence_onset_ms",
        code_location="turn_detector_smart.py:46",
        default=300,
        min=150,
        max=800,
        step=10,
        value_type=int,
        description="Consecutive silence ms required to fire EOU (Smart Turn)",
    ),
    "detectors.smart_turn.silence_rms_threshold": TierBSchemaEntry(
        key="detectors.smart_turn.silence_rms_threshold",
        code_location="turn_detector_smart.py:47",
        default=100,
        min=10,
        max=500,
        step=5,
        value_type=int,
        description="RMS energy gate for silence-candidate detection",
    ),
    # --- Backchannel classifier (backchannel_classifier.py) ---
    "detectors.backchannel.emit_threshold": TierBSchemaEntry(
        key="detectors.backchannel.emit_threshold",
        code_location="backchannel_classifier.py:89",
        default=0.3,
        min=0.10,
        max=0.70,
        step=0.01,
        value_type=float,
        description="Suppress backchannel events below this p_backchannel",
    ),
    # --- Orchestrator (realtime_orchestrator.py) ---
    "orchestrator.proposal_batch_window_ms": TierBSchemaEntry(
        key="orchestrator.proposal_batch_window_ms",
        code_location="realtime_orchestrator.py:175",
        default=600,
        min=40,
        max=1500,
        step=50,
        value_type=int,
        description="T4 grace window for first ThinkerProposal",
    ),
    "orchestrator.hard_cancel_after_ms": TierBSchemaEntry(
        key="orchestrator.hard_cancel_after_ms",
        code_location="realtime_orchestrator.py:176",
        default=120,
        min=50,
        # Capped at 180 to keep spec gate <200ms achievable
        # (docs/architecture-v0.1.md:913). See design doc §11 OQ-1.
        max=180,
        step=5,
        value_type=int,
        description="Time before forcing cancel_generation after graceful stop",
    ),
    "orchestrator.p_speech_thresh": TierBSchemaEntry(
        key="orchestrator.p_speech_thresh",
        code_location="realtime_orchestrator.py:177",
        default=0.5,
        min=0.30,
        max=0.70,
        step=0.01,
        value_type=float,
        description="Onset-detection gate for barge-in",
    ),
    "orchestrator.p_backchannel_thresh": TierBSchemaEntry(
        key="orchestrator.p_backchannel_thresh",
        code_location="realtime_orchestrator.py:178",
        default=0.7,
        min=0.40,
        max=0.90,
        step=0.01,
        value_type=float,
        description="Post-onset gate to avoid stopping playback on backchannel",
    ),
    # --- Background reasoner budgets (background_reasoner.py — v0.2a T3) ---
    "reasoner.budget_wall_clock_s": TierBSchemaEntry(
        key="reasoner.budget_wall_clock_s",
        code_location="companion_harness/background_reasoner.py:201",
        default=30.0,
        min=1.0,
        max=300.0,
        step=1.0,
        value_type=float,
        description=(
            "Wall-clock budget (seconds) per BackgroundReasoner.select_and_call() "
            "invocation; exceed raises BackgroundReasonerBudgetExhausted."
        ),
    ),
    "reasoner.budget_step_count": TierBSchemaEntry(
        key="reasoner.budget_step_count",
        code_location="companion_harness/background_reasoner.py:202",
        default=8,
        min=1,
        max=64,
        step=1,
        value_type=int,
        description=(
            "Maximum MCP steps per BackgroundReasoner.select_and_call() "
            "invocation; exceed raises BackgroundReasonerBudgetExhausted."
        ),
    ),
}


# Spec-frozen Tier-A keys. Patching any of these via /config/patch must be
# rejected (design doc §5 rejection rules). This is not exhaustive of every
# spec-pinned constant in the codebase — it covers the determinism-affecting
# names a dashboard request could plausibly target. New entries land alongside
# the Phase-2 contract test ``test_tier_a_immutability`` (design doc §7).
_TIER_A_KEYS: frozenset[str] = frozenset({
    # Versioning (invariant #5: bumped only by intentional spec migration)
    "POLICY_VERSION",
    "CONFIG_VERSION",
    # ASR determinism trio (greedy, no beam, no autoregressive conditioning)
    "asr.temperature",
    "asr.beam_size",
    "asr.condition_on_previous_text",
    "asr.language",
    # Part 8 acceptance gates (budgets, not knobs)
    "gates.physical_user_speech_onset_to_stop_ms_p95",
    "gates.vad_detected_user_speech_to_stop_ms_p95",
    "gates.direct_question_latency_p50",
    "gates.direct_question_latency_p95",
    "gates.false_interruption_count_per_10_min",
    # Spec Part 6 Stage 3 per-mode tables (speak_policy_config.py)
    "policy.spec_alert_threshold",
    "policy.spec_aesthetic_reaction_budget",
    "policy.spec_eou_policy.interruption_cost",
    "policy.level_to_float_threshold",
})


def tier_a_keys() -> frozenset[str]:
    """Return the spec-frozen Tier-A keys.

    Used by the /config/patch rejection path (design doc §5) to distinguish
    "key not in Tier-B allowlist because it's Tier A" from "unknown key".
    """
    return _TIER_A_KEYS


HOT_SEAMS: tuple[str, ...] = (
    "vad", "smart_turn", "backchannel", "asr", "tts",
    "scene_scorer", "grounding_model", "av_conflict_scorer",
    "urgency_scorer", "embedder", "attachment_risk_monitor",
    "fast_tool_dispatcher",
)


def validate_seam_patch(seam: str, enabled: object) -> tuple[bool, str]:
    """Mirror of validate_patch() for seam toggles."""
    if seam not in HOT_SEAMS:
        return False, f"unknown seam: {seam!r}"
    if not isinstance(enabled, bool):
        return False, f"enabled must be bool, got {type(enabled).__name__}"
    return True, ""


def validate_patch(key: str, value: object) -> tuple[bool, str]:
    """Validate a proposed Tier-B patch.

    Returns ``(True, "")`` if the patch is acceptable, otherwise
    ``(False, error_msg)``. Checks, in order:
      1. ``key`` is in :data:`ALLOWLIST`.
      2. ``value`` is of the entry's ``value_type``.
         (``bool`` is rejected here even though ``bool`` is a subclass of
         ``int`` in Python — a slider value should never be a bool.)
      3. ``value`` is within ``[min, max]``.
    """
    entry = ALLOWLIST.get(key)
    if entry is None:
        return False, f"key not in Tier-B allowlist: {key!r}"

    # Reject bools explicitly: in Python ``isinstance(True, int)`` is True,
    # but a numeric slider should never accept True/False.
    if isinstance(value, bool):
        return False, (
            f"type mismatch for {key!r}: expected {entry.value_type.__name__}, "
            f"got bool"
        )

    if not isinstance(value, entry.value_type):
        return False, (
            f"type mismatch for {key!r}: expected {entry.value_type.__name__}, "
            f"got {type(value).__name__}"
        )

    if value < entry.min or value > entry.max:
        return False, (
            f"value out of range for {key!r}: {value} not in "
            f"[{entry.min}, {entry.max}]"
        )

    return True, ""
