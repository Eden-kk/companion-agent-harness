"""v0.1f event-type payload schema registry (Anchor 2).

Locks the four classification axes — payload_kind, subject_class,
sensitivity, retention_policy_id — for the five new tool_* event types
introduced in v0.1f (Stage 5 — two-tier MCP + evidence-bound filler).
Mirrors the shape of v0_1e_event_schema.py (v0.1e Anchor 3); see that
module for the DERIVED_FROM_* sentinel discipline.

These axes are stamped onto every emitted Event and are expensive to
change retroactively (per v0.1f Anchor 2 durability discipline).
retention_policy_id values referenced here MUST exist in
companion_harness/replay_privacy_policy.yaml.

The five event types and their Anchor-2 ordering invariant (closes via
caused_by[]; invariant #1):

    tool_call_requested
      → tool_call_dispatched
        → tool_progress_event*   (zero or more; each caused_by previous)
          → (tool_call_completed | tool_call_cancelled)

Every payload carries `tool_call_id: str` (a structural identifier,
deterministic across replay per invariant #5) which threads concurrent
in-flight tool calls through ToolProgressEmitter.evidence_at(),
tool_router.cancel(), and BackgroundReasoner.select_and_call().  The
generator is `ToolRouter.dispatch()` (Task 3); this module only declares
the schema, not the generator.

The `tool_progress_event` payload additionally carries `progress_stage`
from the locked alphabet in companion_harness.tool_progress.ProgressStage
(Anchor 1).

The `tool_call_dispatched` payload additionally carries
`routing_tier: Literal["fast", "smart"]` per Anchor 3; the decision is
recorded as one bit of metadata on the dispatch rather than as a
standalone ToolRouterDecision event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Sentinels: value must be derived from the event's payload at emission time.
DERIVED_FROM_REQUEST = None  # tool_call_requested → from request payload
DERIVED_FROM_PROGRESS = None  # tool_progress_event → from tool output payload
DERIVED_FROM_RESULT = None  # tool_call_completed → from result payload

PayloadKind  = Literal["signal", "transcript", "raw_audio", "raw_video",
                       "model_output", "memory_op", "tool_event"]
SubjectClass = Literal["self", "third_party", "mixed", "unknown"]
Sensitivity  = Literal["safe", "sensitive", "highly_sensitive"]


@dataclass(frozen=True)
class ToolEventSchema:
    """Classification axes for one v0.1f tool_* event type (Anchor 2 row)."""
    payload_kind:        PayloadKind
    subject_class:       SubjectClass | None  # None → DERIVED_FROM_*
    sensitivity:         Sensitivity  | None  # None → DERIVED_FROM_*
    retention_policy_id: str          | None  # None → DERIVED_FROM_*
    required_fields:     tuple[str, ...]
    notes:               str


EVENT_TYPE_SCHEMAS: dict[str, ToolEventSchema] = {
    # Anchor 2 row 1: request issued by orchestrator (fast path) OR by
    # background reasoner (smart path).  Sensitivity / retention default
    # to the audit class; sensitive request bodies override at emission
    # time via the DERIVED_FROM_REQUEST sentinel pattern.
    "tool_call_requested": ToolEventSchema(
        payload_kind="tool_event",
        subject_class="self",
        sensitivity=DERIVED_FROM_REQUEST,
        retention_policy_id=DERIVED_FROM_REQUEST,  # default tool_call_audit_30d
        required_fields=("tool_call_id",),
        notes=(
            "Request issued by orchestrator. sensitivity/retention default "
            "to tool_call_audit_30d when the request body is `safe`; "
            "sensitive request bodies derive from request payload at "
            "emission time.  caused_by[] closes through the upstream signal "
            "(e.g., user-utterance) that initiated the call."
        ),
    ),

    # Anchor 2 row 2: dispatcher confirms tool acceptance; carries the
    # Anchor 3 routing_tier label.  Fully static classification.
    "tool_call_dispatched": ToolEventSchema(
        payload_kind="tool_event",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="tool_call_audit_30d",
        required_fields=("tool_call_id", "routing_tier"),
        notes=(
            "Dispatcher acceptance receipt.  routing_tier ∈ {fast, smart} "
            "is recorded here per Anchor 3 (replay-deterministic; same "
            "PolicyInputs + same tool-request → same routing_tier).  "
            "caused_by[] cites the preceding tool_call_requested."
        ),
    ),

    # Anchor 2 row 3 + Anchor 1 alphabet: per-progress event carrying the
    # locked progress_stage label.  Sensitivity / retention derive from
    # the tool output payload (a 'scanning 12k records' progress event is
    # safe; a progress event leaking partial PII is not).
    "tool_progress_event": ToolEventSchema(
        payload_kind="tool_event",
        subject_class="self",
        sensitivity=DERIVED_FROM_PROGRESS,
        retention_policy_id=DERIVED_FROM_PROGRESS,  # default tool_call_audit_30d
        required_fields=("tool_call_id", "progress_stage"),
        notes=(
            "Per-progress event with progress_stage from the locked "
            "companion_harness.tool_progress.ProgressStage alphabet "
            "(Anchor 1).  sensitivity/retention default to "
            "tool_call_audit_30d when the progress body is `safe`; "
            "sensitive progress bodies (e.g., partial query results) "
            "derive at emission time.  caused_by[] closes through the "
            "previous tool_progress_event or the tool_call_dispatched."
        ),
    ),

    # Anchor 2 row 4: receipt + result reference; result body governance
    # is separate (the result content lives behind its own retention id).
    "tool_call_completed": ToolEventSchema(
        payload_kind="tool_event",
        subject_class="self",
        sensitivity=DERIVED_FROM_RESULT,
        retention_policy_id=DERIVED_FROM_RESULT,  # default tool_call_audit_30d
        required_fields=("tool_call_id",),
        notes=(
            "Completion receipt with a reference to the result body.  "
            "sensitivity/retention default to tool_call_audit_30d for the "
            "receipt; the result body itself is governed by its own policy "
            "id (e.g., tool_result_default for `safe` results)."
        ),
    ),

    # Anchor 2 row 5: cancellation receipt; cause via caused_by[]
    # (typically the vad_user_speech_onset event that triggered barge-in).
    # Fully static classification.
    "tool_call_cancelled": ToolEventSchema(
        payload_kind="tool_event",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="tool_call_audit_30d",
        required_fields=("tool_call_id",),
        notes=(
            "Cancellation receipt.  caused_by[] cites the upstream cause "
            "(typically vad_user_speech_onset for barge-in; v0.1f scopes "
            "cancellation to barge-in only per OQ-10).  No content payload "
            "beyond the structural identifier."
        ),
    ),
}
