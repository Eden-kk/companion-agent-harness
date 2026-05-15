"""Concrete dataclasses / enums for harness type signatures.

See docs/architecture-v0.1.md §Part 5 for the type signatures
(Event, DecisionTrace, TurnSignal, PolicyInputs, SpeakDecision,
ThinkerProposal, MemoryItem, EvaluationCase, ReplayRun) and §Part 4 for
the SensitiveField + companion_state schema.  ReasonCode is defined in
companion_harness.reason_codes; import it from there.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from companion_harness.reason_codes import ReasonCode  # intentionally not re-exported; import from reason_codes

__all__ = [
    "SensitiveField",
    "Event",
    "DecisionTrace",
    "TurnSignal",
    "PolicyInputs",
    "SpeakDecision",
    "ThinkerProposal",
    "MemoryItem",
    "EvaluationCase",
    "ReplayRun",
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
    subject_class:       Literal["self", "third_party", "mixed", "unknown"]
    sensitivity:         Literal["safe", "sensitive", "highly_sensitive"]
    retention_policy_id: str


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


@dataclass
class MemoryItem:
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
    user_visible_summary: str


@dataclass
class EvaluationCase:
    case_id:          str
    stage:            int
    scenario:         str
    modalities:       list[str]
    fixture_ref:      str
    expected_events:  list[str]
    expected_metrics: dict
    consent_class:    str


@dataclass
class ReplayRun:
    run_id:                       str
    case_id:                      str
    implementation_config_version: str
    policy_version:               str
    started_at:                   str
    finished_at:                  str | None
    results:                      dict
    failures:                     list[dict]
