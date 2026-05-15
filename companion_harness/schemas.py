"""Concrete dataclasses / enums for harness type signatures.

See docs/architecture-v0.1.md §Part 5 for the type signatures
(Event, DecisionTrace, TurnSignal, PolicyInputs, SpeakDecision,
ThinkerProposal, MemoryItem, EvaluationCase, ReplayRun) and §Part 4 for
the SensitiveField + companion_state schema.  ReasonCode is defined in
companion_harness.reason_codes; import it from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from companion_harness.reason_codes import ReasonCode  # intentionally not re-exported; import from reason_codes
from companion_harness.tool_progress import ToolProgressEvidence  # v0.1f Anchor 4 (re-exported via __all__)

__all__ = [
    "SensitiveField",
    "Event",
    "DecisionTrace",
    "TurnSignal",
    "PolicyInputs",
    "SpeakDecision",
    "ThinkerProposal",
    "RubricViolation",
    "TrackedSignal",
    "AttachmentRiskSignal",
    "MemoryItem",
    "EvaluationCase",
    "ReplayRun",
    "ToolProgressEvidence",
    "ResponseContentSource",
]


ResponseContentSource = Literal[
    "no_synthesis",
    "foreground_response_proposal",
    "backchannel",
    "filler_with_tool_evidence",
    "filler_without_evidence",
    "operator_injected",
]


# --- v0.1g Stage 6 (companion texture) schema additions ----------------------
#
# Aesthetic-reaction rubric (Anchor 1 of docs/roadmap-v0.1g-draft.md):
#   Spec lines 624-658 enumerate ten PASS criteria.  Eight map to discrete
#   violation IDs and ride on ThinkerProposal.rubric_violations as durable
#   metadata.  The remaining two (cooldown, task-derail) are already covered
#   by COOLDOWN_BLOCKED / QUIET_MODE_BLOCKED.
#
# Attachment-risk signals (Anchor 2 of the same draft):
#   Spec lines 843-857 enumerate six tracked signals.  Each detected
#   occurrence emits one attachment_risk_signal event whose payload is
#   AttachmentRiskSignal; PolicyInputs.attachment_risk_level stays the
#   policy-layer-facing scalar (pure function of these events -- preserves
#   invariant #5 deterministic replay).
#
# Enum-string spellings are durable metadata once events carrying them land
# on disk; renaming costs a one-time reclassification pass.


RubricViolation = Literal[
    "RUBRIC_TOO_LONG",                 # spec line 624 ("<= 8 words")
    "RUBRIC_UNGROUNDED",               # spec ("grounded in available sensors")
    "RUBRIC_POSSESSIVE",               # spec ("non-possessive")
    "RUBRIC_DIAGNOSTIC",               # spec ("non-diagnostic")
    "RUBRIC_FLATTERING",               # spec ("non-flattering")
    "RUBRIC_FABRICATED_MEMORY",        # spec corollary ("no fabricated personal memory")
    "RUBRIC_ABSENT_SENSORY_CHANNEL",   # spec corollary ("no claimed absent sensor")
    "RUBRIC_IDENTITY_ONLY",            # spec corollary ("references texture, not just identity")
]


TrackedSignal = Literal[
    "prolonged_daily_use_minutes",     # spec line 843
    "emotional_exclusivity_signals",   # spec line 844
    "user_says_ai_is_only_friend",     # spec line 845
    "repeated_reassurance_loops",      # spec line 846
    "reduced_human_contact_mentions",  # spec line 847
    "explicit_crisis_signals",         # spec line 848 (highly_sensitive)
]


@dataclass
class SensitiveField:
    retention_policy_id: str
    value:               str | None = None
    value_ref:           str | None = None
    redacted_value:      str | None = None
    sensitivity:         Literal["safe", "sensitive", "highly_sensitive"] = "sensitive"
    source_event_ids:    list[str] = field(default_factory=list)


@dataclass
class Event:
    event_id:            str
    session_id:          str
    schema_version:      str
    seq_no:              int
    event_type:          str
    timestamp_mono_ms:   int
    timestamp_wall:      str
    source:              str
    caused_by:           list[str]
    payload_hash:        str
    payload_ref:         str | None
    payload_kind:        Literal["signal", "transcript", "raw_audio", "raw_video",
                                 "model_output", "memory_op", "tool_event"]
    subject_class:       Literal["self", "third_party", "mixed", "unknown", "operator"]
    sensitivity:         Literal["safe", "sensitive", "highly_sensitive"]
    retention_policy_id: str
    # Optional inline payload for fields surfaced alongside the envelope so
    # display/audit consumers don't need to dereference payload_ref. Used by
    # policy_decision (action_type + primary_reason_code). Must contain only
    # deterministic, non-sensitive fields — see Finding 5 in
    # docs/manual-test-findings-2026-05-15.md.
    payload_inline:      dict | None = None


@dataclass
class DecisionTrace:
    decision_id:             str
    input_event_ids:         list[str]
    signal_event_ids:        list[str]
    threshold_path:          list[str]
    primary_reason_code:     ReasonCode
    supporting_reason_codes: list[ReasonCode]
    counterfactuals:         dict
    redacted_explanation:    str | None
    sensitive_explanation_ref: str | None
    policy_version:          str
    config_version:          str
    model_adapter_versions:  dict[str, str]
    retrieval_used:          list[str] = field(default_factory=list)


@dataclass
class TurnSignal:
    detector:          str
    p_done:            float
    p_continue:        float
    p_backchannel:     float
    confidence:        float
    evidence_event_ids: list[str]


@dataclass
class PolicyInputs:
    user_speaking:               bool
    eou_probability:             float
    assistant_speaking:          bool
    scene_change_score:          float
    deictic_reference:           bool
    user_addressed_agent:        bool
    urgency_score:               float
    proactivity_budget_remaining: dict[str, int]
    privacy_mode:                str
    current_task_mode:           str
    social_mode:                 str
    risk_mode:                   str
    cooldown_state:              dict[str, int]
    attachment_risk_level:       float
    audio_visual_conflict_score: float = 0.0
    grounding_confidence:        float = 1.0
    deictic_ambiguous:           bool  = False
    quiet_mode_active:           bool  = False
    aesthetic_novelty_score:     float = 0.0
    short_response_appropriate:  bool  = False
    retrieved_items:             list["MemoryItem"] = field(default_factory=list)
    tool_progress_evidence:      ToolProgressEvidence | None = None  # v0.1f Anchor 4
    user_transcript:             str = ""  # ASR output for the current/just-completed turn. v0.1f addition.


@dataclass
class SpeakDecision:
    action_type:             Literal[
                                 "silence", "backchannel", "short_reaction", "full_response",
                                 "clarification", "alert", "tool_status", "aesthetic_reaction"
                             ]
    primary_reason_code:     ReasonCode
    supporting_reason_codes: list[ReasonCode]
    redacted_explanation:    str | None
    caused_by:               list[str]
    budget_bucket:           str | None
    allowed_prosody_tags:    list[str]
    max_duration_ms:         int | None
    response_content_source: ResponseContentSource = "no_synthesis"


@dataclass
class ThinkerProposal:
    proposal_type:     Literal["observation", "question", "aesthetic_reaction", "memory_bridge"]
    content:           str
    trigger:           str
    confidence:        float
    novelty:           float
    interruption_cost: float
    max_utterance_ms:  int
    cooldown_consumed: str
    caused_by:         list[str]
    # v0.1g Anchor 1: empty list = rubric passes; non-empty list = list of
    # check IDs that fired.  Per Anchor 4 the rubric runs inside
    # SpeakPolicy.decide(); the proposal carries the result so it appears on
    # the event log (invariant #1: spec line 663 "logged with which criteria
    # fired").
    rubric_violations: list[RubricViolation] = field(default_factory=list)


@dataclass
class AttachmentRiskSignal:
    """Per-event observation contributing to attachment_risk_level (Anchor 2).

    Emitted as the payload of an `attachment_risk_signal` event by the
    AttachmentRiskMonitor (v0.1g Task 7).  PolicyInputs.attachment_risk_level
    is the scalar aggregate; this dataclass is the durable per-occurrence
    record (invariant #1 visibility + spec line 840 "fires only on
    high-confidence signals").
    """

    signal_class:        TrackedSignal
    confidence:          float
    evidence_event_ids:  list[str]


@dataclass
class MemoryItem:
    """Provenance-complete memory record (invariant #3 / Part 5).

    Field-to-invariant mapping (invariant #3: "Every memory item carries
    source_event_id, created_at, confidence, salience, valid_from/valid_to,
    superseded_by, and a user_visible_summary"):

      source_event_id    — causal anchor; closes the event DAG (invariant #1).
      created_at         — timestamp required by invariant #3.
      confidence         — required by invariant #3; drives retrieval ranking.
      salience           — required by invariant #3; drives retrieval ranking.
      valid_from         — required by invariant #3.
      valid_to           — required by invariant #3; set on forget/expiry →
                           serves test_explicit_forget + test_correction.
      superseded_by      — item_id of the correction record that replaces this
                           one → serves test_explicit_forget + test_correction.
      user_visible_summary — human-readable provenance summary required by
                           invariant #3; routed through SensitiveField because
                           it may embed PII (CLAUDE.md "Free-text fields go
                           through SensitiveField") → serves
                           test_why_did_you_say_that.
    """

    item_id:            str
    store:              Literal["session", "core_profile", "episodic", "semantic_relational"]
    content:            dict
    source_event_id:    str
    created_at:         str
    last_confirmed_at:  str
    confidence:         float
    salience:           float
    privacy_level:      str
    mutability:         Literal["frozen", "user_only", "system_revisable"]
    valid_from:         str
    valid_to:           str | None
    superseded_by:      str | None
    user_visible_summary: SensitiveField


@dataclass
class EvaluationCase:
    """Fixture/CI-oriented case descriptor (architecture-v0.1.md §Part 5).

    The 8 required positional fields are the original spec-frozen shape.
    The 5 optional fields below were added in Task A1 (eval-subsystem-spec.md
    Anchor 3) to support benchmark adapters without breaking legacy call sites.

    Key mapping note: fixture case.json files use the key ``sensitivity``; the
    field name here is ``consent_class``.  When loading from JSON, callers must
    map ``case_json["sensitivity"] -> EvaluationCase.consent_class``.
    """

    case_id:          str
    stage:            int
    scenario:         str
    modalities:       list[str]
    fixture_ref:      str
    expected_events:  list[str]
    expected_metrics: dict
    consent_class:    str
    # --- eval-subsystem fields (Task A1 / plan-eval-phase-a-execution.md) ---
    benchmark_name:     str | None = None
    benchmark_version:  str | None = None
    inputs:             dict | None = None
    expected_behavior:  dict | None = None
    fixtures:           list[str] = field(default_factory=list)


@dataclass
class ReplayRun:
    """Replay-run record (architecture-v0.1.md §Part 5).

    The 8 required positional fields are the original spec-frozen shape.
    The 6 optional fields below were added in Task A1 (eval-subsystem-spec.md
    Anchor 3) to carry eval-subsystem metadata.

    ``decisions`` defaults to an empty list and will be populated by
    ``companion_harness.replay.run_tier_b_replay()`` (Phase A.5+); it remains
    empty in Phase A.
    """

    run_id:                       str
    case_id:                      str
    implementation_config_version: str
    policy_version:               str
    started_at:                   str
    finished_at:                  str | None
    results:                      dict
    failures:                     list[dict]
    # --- eval-subsystem fields (Task A1 / plan-eval-phase-a-execution.md) ---
    event_log_path:       Path | None = None
    decisions:            list["DecisionTrace"] = field(default_factory=list)
    timing_mode:          Literal["synthetic_clock", "wall_clock"] | None = None
    started_at_mono_ms:   int | None = None
    completed_at_mono_ms: int | None = None
    final_status:         Literal["completed", "skipped", "cancelled", "error"] | None = None
