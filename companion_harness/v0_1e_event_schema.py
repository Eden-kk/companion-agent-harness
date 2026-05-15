"""v0.1e event-type payload schema registry (Anchor 3).

Locks the four classification axes — payload_kind, subject_class,
sensitivity, retention_policy_id — for the five new memory_* event types
introduced in v0.1e.  These axes are stamped onto every emitted Event and
are expensive to change retroactively (see roadmap-v0.1e-draft.md Anchor 3).

Design choice for "derived from MemoryItem" cells
--------------------------------------------------
Three cells in the Anchor 3 table say "derived from MemoryItem.<field>".
This registry represents them as None with a sentinel constant
DERIVED_FROM_ITEM.  Emission code MUST substitute the actual value at
construction time (from the MemoryItem the event wraps); it MUST NOT
hardcode a static value.  Using None lets static type-checkers flag any
callsite that accidentally passes the sentinel directly into an Event field
(which expects a non-None str Literal).

The three fully-static event types (memory_retrieval_event,
audit_tombstone_write, deletion_receipt_write) carry their classification
values directly as str literals so they can be validated without a
MemoryItem in hand.  memory_commit_completed is mixed: its subject_class
derives from the committed item (DERIVED_FROM_ITEM) while the other axes
are static.

retention_policy_id values are the stable enum names locked in Anchor 2;
their TTL is config-only (replay_privacy_policy.yaml).

All sensitivity values are exact Literal["safe", "sensitive",
"highly_sensitive"] — no parenthetical annotations (per Anchor 3 note).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Sentinel: value must be derived from the MemoryItem wrapped by the event.
DERIVED_FROM_ITEM = None

PayloadKind  = Literal["signal", "transcript", "raw_audio", "raw_video",
                        "model_output", "memory_op", "tool_event"]
SubjectClass = Literal["self", "third_party", "mixed", "unknown"]
Sensitivity  = Literal["safe", "sensitive", "highly_sensitive"]


@dataclass(frozen=True)
class MemoryEventSchema:
    """Classification axes for one memory_* event type (Anchor 3 table row).

    Fields that say DERIVED_FROM_ITEM (None) must be resolved from the
    wrapped MemoryItem at emission time.
    """
    payload_kind:        PayloadKind
    subject_class:       SubjectClass | None  # None → DERIVED_FROM_ITEM
    sensitivity:         Sensitivity  | None  # None → DERIVED_FROM_ITEM
    retention_policy_id: str          | None  # None → DERIVED_FROM_ITEM
    notes:               str


EVENT_TYPE_SCHEMAS: dict[str, MemoryEventSchema] = {
    # per-content event: all three variable axes come from the MemoryItem
    "memory_write_candidate": MemoryEventSchema(
        payload_kind="memory_op",
        subject_class=DERIVED_FROM_ITEM,    # MemoryItem.subject_class
        sensitivity=DERIVED_FROM_ITEM,      # MemoryItem.privacy_level → Sensitivity mapping
        retention_policy_id=DERIVED_FROM_ITEM,  # Anchor 2 alphabet; varies by content
        notes=(
            "Per-content classification. subject_class and sensitivity are "
            "set on the wrapper Event from MemoryItem.subject_class and "
            "MemoryItem.privacy_level respectively. retention_policy_id is "
            "also derived (e.g. ep_default_30d for episodic items)."
        ),
    ),

    # query metadata only; retrieved item content lives in MemoryItem records
    "memory_retrieval_event": MemoryEventSchema(
        payload_kind="memory_op",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="retrieval_audit_30d",
        notes=(
            "Query metadata only. Retrieved item content lives in separate "
            "MemoryItem records, not in this event's payload. "
            "DecisionTrace.retrieval_used references event_ids of these events."
        ),
    ),

    # skip-receipt; no content (guest_present gate)
    "memory_commit_skipped": MemoryEventSchema(
        payload_kind="memory_op",
        subject_class=DERIVED_FROM_ITEM,    # from would-be MemoryItem
        sensitivity="safe",
        retention_policy_id="commit_audit_30d",
        notes=(
            "Skip receipt when guest_present gate suppresses a commit. "
            "No content payload. subject_class derived from the would-be MemoryItem."
        ),
    ),

    # receipt only; no content
    "memory_commit_completed": MemoryEventSchema(
        payload_kind="memory_op",
        subject_class=DERIVED_FROM_ITEM,    # from committed MemoryItem
        sensitivity="safe",
        retention_policy_id="commit_audit_30d",
        notes=(
            "Commit receipt; no content payload. subject_class derived from "
            "the committed MemoryItem."
        ),
    ),

    # forget-receipt; no content per spec lines 525-526
    "audit_tombstone_write": MemoryEventSchema(
        payload_kind="memory_op",
        subject_class=DERIVED_FROM_ITEM,    # from tombstoned MemoryItem
        sensitivity="safe",
        retention_policy_id="audit_indefinite",
        notes=(
            "Forget-receipt (tombstone). No content per spec lines 525-526 "
            "'minimal audit tombstone retained'. subject_class derived from "
            "the tombstoned MemoryItem."
        ),
    ),

    # hard-delete-receipt; no content per spec lines 532-533
    "deletion_receipt_write": MemoryEventSchema(
        payload_kind="memory_op",
        subject_class=DERIVED_FROM_ITEM,    # from deleted MemoryItem
        sensitivity="safe",
        retention_policy_id="audit_indefinite",
        notes=(
            "Hard-delete receipt. No content per spec lines 532-533. "
            "subject_class derived from the deleted MemoryItem."
        ),
    ),

    # policy-decision trace; all fields are policy-replay-safe (spec lines 365-381)
    "decision_trace_emitted": MemoryEventSchema(
        payload_kind="model_output",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="decision_trace_30d",
        notes=(
            "DecisionTrace artifact co-emitted with policy_decision. "
            "All fields (threshold_path, counterfactuals, reason codes, versions) "
            "are Tier B replay-safe. redacted_explanation is None in v0.1e. "
            "decision_id links to the paired policy_decision event payload."
        ),
    ),
}
