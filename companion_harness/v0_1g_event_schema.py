"""v0.1g event-type payload schema registry (Anchor 2 / §New event-type payload schema).

Locks the four classification axes -- payload_kind, subject_class,
sensitivity, retention_policy_id -- for the four new event types
introduced in v0.1g (Stage 6 -- companion texture).  Mirrors the shape of
v0_1e_event_schema.py (v0.1e Anchor 3); see that module for the
DERIVED_FROM_ITEM sentinel discipline.

These axes are stamped onto every emitted Event and are expensive to
change retroactively (per v0.1g Anchor 2 / Anchor-2-from-v0.1e durability
discipline).  retention_policy_id values referenced here MUST exist in
companion_harness/replay_privacy_policy.yaml.

The four event types:

  aesthetic_proposal_generated   per Anchor 4 -- emitted for EVERY proposal
                                 (rubric-pass or rubric-fail) so the
                                 spec-line-663 "which criteria fired" is
                                 on the event log.
  attachment_risk_signal         per Anchor 2 -- one event per detected
                                 tracked-signal occurrence (six classes,
                                 spec lines 843-848).  Sensitivity is
                                 DERIVED_FROM_PAYLOAD because
                                 explicit_crisis_signals -> highly_sensitive,
                                 others -> sensitive.
  recent_shared_moment_referenced  reuses retrieval_audit_30d retention
                                 (v0.1e); wraps the projection retrieval
                                 over episodic_memory (Anchor 3).
  user_reduction_command_applied logs the budget mutation; reuses
                                 commit_audit_30d retention (v0.1e).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Sentinel: value must be derived from the event's payload at emission time.
# Used for attachment_risk_signal.sensitivity (depends on signal_class).
DERIVED_FROM_PAYLOAD = None

PayloadKind  = Literal["signal", "transcript", "raw_audio", "raw_video",
                       "model_output", "memory_op", "tool_event"]
SubjectClass = Literal["self", "third_party", "mixed", "unknown"]
Sensitivity  = Literal["safe", "sensitive", "highly_sensitive"]


@dataclass(frozen=True)
class StageSixEventSchema:
    """Classification axes for one v0.1g event type."""
    payload_kind:        PayloadKind
    subject_class:       SubjectClass | None  # None -> DERIVED_FROM_PAYLOAD
    sensitivity:         Sensitivity  | None  # None -> DERIVED_FROM_PAYLOAD
    retention_policy_id: str
    notes:               str


EVENT_TYPE_SCHEMAS: dict[str, StageSixEventSchema] = {
    # Anchor 4: every proposal logged pre-policy so rubric attribution is
    # replayable (spec line 663).  Sensitivity defaults to `sensitive` because
    # the payload carries free-text content.
    "aesthetic_proposal_generated": StageSixEventSchema(
        payload_kind="model_output",
        subject_class="self",
        sensitivity="sensitive",
        retention_policy_id="proposal_audit_30d",
        notes=(
            "Emitted for EVERY aesthetic-reaction proposal (rubric-pass or "
            "rubric-fail) per Anchor 4 + invariant #1.  Payload includes the "
            "ThinkerProposal (incl. rubric_violations).  Sensitivity is "
            "`sensitive` because content is free-text model output."
        ),
    ),

    # Anchor 2: per-detection event.  Six signal classes; explicit_crisis_signals
    # is the highly_sensitive bucket, all others are sensitive.
    "attachment_risk_signal": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity=DERIVED_FROM_PAYLOAD,
        retention_policy_id="attachment_risk_audit_365d",
        notes=(
            "One event per detected tracked-signal occurrence (spec lines "
            "843-848).  Sensitivity derived from AttachmentRiskSignal.signal_class "
            "at emission time: explicit_crisis_signals -> highly_sensitive, "
            "others -> sensitive.  365-day retention reflects the higher audit "
            "bar for distress signals."
        ),
    ),

    # Anchor 3 / Task 8: wraps a memory_retrieval_event filtered to the
    # shared-moments path; reuses the v0.1e retrieval_audit_30d retention
    # policy (no new ID needed for this event type).
    "recent_shared_moment_referenced": StageSixEventSchema(
        payload_kind="memory_op",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="retrieval_audit_30d",
        notes=(
            "Wraps a memory_retrieval_event filtered to the shared-moments "
            "projection over episodic_memory (Anchor 3).  Carries the "
            "retrieved item event_ids; durable content lives in MemoryItem "
            "records.  Reuses v0.1e retention policy."
        ),
    ),

    # Task 9 (Wave 5): receipt for user reduction command compliance
    # (spec line 678-680).  Reuses commit_audit_30d retention.
    "user_reduction_command_applied": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="commit_audit_30d",
        notes=(
            "Logs the budget mutation triggered by a user reduction command "
            "(\"less proactive\" / \"quiet mode\").  Payload carries the verb "
            "and the resulting mutation; caused_by[] closes through the user "
            "input event.  Reuses v0.1e retention policy."
        ),
    ),
}
