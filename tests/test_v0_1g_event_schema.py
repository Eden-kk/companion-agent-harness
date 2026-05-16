"""v0.1g Task 1+2 foundation tests.

Schema completeness + enum stability checks for the v0.1g (Stage 6 --
companion texture) foundation PR.  Per CLAUDE.md coding rule 4, this PR's
verifiable success criterion is "these tests pass without behavior change
to downstream code."  Downstream behavior tests (rubric, attachment-risk,
recent_shared_moments) ship in their respective task PRs (Wave 3-6).

Covered:

  - The four new event_type schemas (Anchor 2) are present with the
    required classification axes (payload_kind, subject_class,
    sensitivity, retention_policy_id) populated per the §New event-type
    payload schema table.
  - The 8 RubricViolation IDs (Anchor 1) are present as a Literal type
    alias and spell exactly as the spec requires.
  - The 6 TrackedSignal IDs (Anchor 2) match the spec line 843-848 set.
  - The 3 new ReasonCode members exist with exact spellings (audit per
    docs/roadmap-v0.1g-draft.md §Wave 1 / Task 2: 3 ADD, 2 DO NOT ADD).
  - ThinkerProposal.rubric_violations defaults to an empty list (Anchor 1
    semantics: empty == rubric passes).
  - The two new retention_policy_id values are present in
    replay_privacy_policy.yaml.
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import yaml

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    AttachmentRiskSignal,
    RubricViolation,
    ThinkerProposal,
    TrackedSignal,
)
from companion_harness.v0_1g_event_schema import (
    DERIVED_FROM_PAYLOAD,
    EVENT_TYPE_SCHEMAS,
    StageSixEventSchema,
)


# --- event-type schema registry ---------------------------------------------

EXPECTED_EVENT_TYPES = {
    "aesthetic_proposal_generated",
    "attachment_risk_signal",
    "recent_shared_moment_referenced",
    "user_reduction_command_applied",
    "native_duplex_invocation",
}


def test_event_type_schemas_cover_all_new_types() -> None:
    assert set(EVENT_TYPE_SCHEMAS.keys()) == EXPECTED_EVENT_TYPES


def test_each_schema_has_required_axes() -> None:
    for event_type, schema in EVENT_TYPE_SCHEMAS.items():
        assert isinstance(schema, StageSixEventSchema), event_type
        assert schema.payload_kind in {
            "signal", "transcript", "raw_audio", "raw_video",
            "model_output", "memory_op", "tool_event",
        }, event_type
        # subject_class may be DERIVED_FROM_PAYLOAD (None) but for all four
        # v0.1g event types we lock it to a concrete value.
        assert schema.subject_class is not None, event_type
        # sensitivity may be DERIVED_FROM_PAYLOAD for attachment_risk_signal.
        if event_type == "attachment_risk_signal":
            assert schema.sensitivity is DERIVED_FROM_PAYLOAD
        else:
            assert schema.sensitivity in {"safe", "sensitive", "highly_sensitive"}, event_type
        assert schema.retention_policy_id, event_type
        assert schema.notes, event_type


def test_aesthetic_proposal_generated_axes() -> None:
    s = EVENT_TYPE_SCHEMAS["aesthetic_proposal_generated"]
    assert s.payload_kind == "model_output"
    assert s.subject_class == "self"
    assert s.sensitivity == "sensitive"
    assert s.retention_policy_id == "proposal_audit_30d"


def test_attachment_risk_signal_axes() -> None:
    s = EVENT_TYPE_SCHEMAS["attachment_risk_signal"]
    assert s.payload_kind == "signal"
    assert s.subject_class == "self"
    # sensitivity is derived from the AttachmentRiskSignal.signal_class at
    # emission time (explicit_crisis_signals -> highly_sensitive, else sensitive).
    assert s.sensitivity is DERIVED_FROM_PAYLOAD
    assert s.retention_policy_id == "attachment_risk_audit_365d"


def test_recent_shared_moment_referenced_axes() -> None:
    s = EVENT_TYPE_SCHEMAS["recent_shared_moment_referenced"]
    assert s.payload_kind == "memory_op"
    assert s.subject_class == "self"
    assert s.sensitivity == "safe"
    # reuses v0.1e retention; not a v0.1g-new ID.
    assert s.retention_policy_id == "retrieval_audit_30d"


def test_user_reduction_command_applied_axes() -> None:
    s = EVENT_TYPE_SCHEMAS["user_reduction_command_applied"]
    assert s.payload_kind == "signal"
    assert s.subject_class == "self"
    assert s.sensitivity == "safe"
    # reuses v0.1e retention; not a v0.1g-new ID.
    assert s.retention_policy_id == "commit_audit_30d"


# --- RubricViolation enum (Anchor 1; 8 IDs) ---------------------------------

EXPECTED_RUBRIC_VIOLATIONS = {
    "RUBRIC_TOO_LONG",
    "RUBRIC_UNGROUNDED",
    "RUBRIC_POSSESSIVE",
    "RUBRIC_DIAGNOSTIC",
    "RUBRIC_FLATTERING",
    "RUBRIC_FABRICATED_MEMORY",
    "RUBRIC_ABSENT_SENSORY_CHANNEL",
    "RUBRIC_IDENTITY_ONLY",
}


def test_rubric_violation_literal_has_eight_ids() -> None:
    members = set(get_args(RubricViolation))
    assert members == EXPECTED_RUBRIC_VIOLATIONS


# --- TrackedSignal enum (Anchor 2; 6 IDs from spec lines 843-848) -----------

EXPECTED_TRACKED_SIGNALS = {
    "prolonged_daily_use_minutes",
    "emotional_exclusivity_signals",
    "user_says_ai_is_only_friend",
    "repeated_reassurance_loops",
    "reduced_human_contact_mentions",
    "explicit_crisis_signals",
}


def test_tracked_signal_literal_has_six_ids() -> None:
    members = set(get_args(TrackedSignal))
    assert members == EXPECTED_TRACKED_SIGNALS


# --- new ReasonCode members (3 ADD per Wave 1 / Task 2 audit) ---------------


def test_new_reason_codes_exist() -> None:
    # ADDED per v0.1g Task 2 gap audit.
    assert ReasonCode.RUBRIC_VIOLATION.value == "RUBRIC_VIOLATION"
    assert ReasonCode.ATTACHMENT_RISK_DAMPEN.value == "ATTACHMENT_RISK_DAMPEN"
    assert ReasonCode.USER_REDUCTION_COMMAND_APPLIED.value == "USER_REDUCTION_COMMAND_APPLIED"


def test_explicitly_rejected_reason_codes_absent() -> None:
    # DO NOT ADD per the same audit -- guard against accidental drift.
    code_names = {c.name for c in ReasonCode}
    assert "SHARED_MOMENT_RETRIEVED" not in code_names
    assert "THINKER_NO_DIRECT_SPEECH_VIOLATION" not in code_names


# --- ThinkerProposal.rubric_violations default ------------------------------


def test_thinker_proposal_rubric_violations_defaults_to_empty_list() -> None:
    p = ThinkerProposal(
        proposal_type="aesthetic_reaction",
        content="the light",
        trigger="scene_change",
        confidence=0.8,
        novelty=0.7,
        interruption_cost=0.2,
        max_utterance_ms=1200,
        cooldown_consumed="aesthetic_reaction",
        caused_by=["upstream-evt-1"],
    )
    assert p.rubric_violations == []


def test_thinker_proposal_accepts_rubric_violations() -> None:
    p = ThinkerProposal(
        proposal_type="aesthetic_reaction",
        content="that's a really beautiful shirt you have on you",
        trigger="scene_change",
        confidence=0.8,
        novelty=0.7,
        interruption_cost=0.2,
        max_utterance_ms=1200,
        cooldown_consumed="aesthetic_reaction",
        caused_by=["upstream-evt-1"],
        rubric_violations=["RUBRIC_TOO_LONG", "RUBRIC_POSSESSIVE", "RUBRIC_FLATTERING"],
    )
    assert p.rubric_violations == ["RUBRIC_TOO_LONG", "RUBRIC_POSSESSIVE", "RUBRIC_FLATTERING"]


# --- AttachmentRiskSignal payload -------------------------------------------


def test_attachment_risk_signal_construction() -> None:
    s = AttachmentRiskSignal(
        signal_class="explicit_crisis_signals",
        confidence=0.95,
        evidence_event_ids=["transcript-evt-7"],
    )
    assert s.signal_class == "explicit_crisis_signals"
    assert s.confidence == 0.95
    assert s.evidence_event_ids == ["transcript-evt-7"]


# --- replay_privacy_policy.yaml retention-ID registry -----------------------


def _load_retention_policy_ids() -> set[str]:
    yaml_path = (
        Path(__file__).resolve().parent.parent
        / "companion_harness"
        / "replay_privacy_policy.yaml"
    )
    with yaml_path.open() as fh:
        doc = yaml.safe_load(fh)
    return set(doc["retention_policies"].keys())


def test_new_retention_policy_ids_present() -> None:
    ids = _load_retention_policy_ids()
    # v0.1g Anchor 2 additions.
    assert "proposal_audit_30d" in ids
    assert "attachment_risk_audit_365d" in ids


def test_carried_retention_policy_ids_preserved() -> None:
    ids = _load_retention_policy_ids()
    # v0.1e carries (must not regress).
    for carried in (
        "retrieval_audit_30d",
        "commit_audit_30d",
        "audit_indefinite",
        "decision_trace_30d",
    ):
        assert carried in ids


def test_v0_1g_event_schemas_reference_only_known_retention_ids() -> None:
    yaml_ids = _load_retention_policy_ids()
    for event_type, schema in EVENT_TYPE_SCHEMAS.items():
        assert schema.retention_policy_id in yaml_ids, (
            f"{event_type} references unknown retention_policy_id "
            f"{schema.retention_policy_id!r}"
        )
