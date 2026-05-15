"""v0.1f event-type payload schema registry (Anchor 2).

Locks the four classification axes — payload_kind, subject_class,
sensitivity, retention_policy_id — for the five new tool_* event types
introduced in v0.1f (Stage 5 — two-tier MCP + evidence-bound filler) plus
the `asr_transcript_emitted` audit event added for invariant #1 (the ASR
transcript drives `_detect_explicit_remember` and the addressing classifier,
so its provenance must be logged).
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
SubjectClass = Literal["self", "third_party", "mixed", "unknown", "operator"]
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

    # docs/design-config-and-dashboard.md §8 row 1: operator HTTP request to
    # the manual-test dashboard's /config/patch + /config/reset endpoints.
    # subject_class="operator" per §11 OQ-4 resolution (new subject class).
    # Payload is structural metadata only; the request body (the patch) lives
    # downstream on the config_change event so we don't duplicate the diff.
    "operator_action": ToolEventSchema(
        payload_kind="signal",
        subject_class="operator",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
        required_fields=("endpoint", "client_ip", "request_id"),
        notes=(
            "Upstream audit event for operator HTTP requests against the "
            "manual-test dashboard's config endpoints (docs/design-config-"
            "and-dashboard.md §8).  Payload carries structural metadata only "
            "(endpoint, client_ip, request_id); the patch body lives on the "
            "downstream config_change event.  caused_by[] is the entry-point "
            "of an operator-initiated chain."
        ),
    ),

    # docs/design-config-and-dashboard.md §8 row 2: downstream config-patch
    # application emitted by /config/patch + /config/reset.  Tier-B values
    # only (numeric thresholds; sensitivity="safe").  caused_by[] always
    # cites the upstream operator_action via operator_action_event_id.
    "config_change": ToolEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
        required_fields=(
            "key",
            "previous_value",
            "new_value",
            "applied_at_ms",
            "operator_action_event_id",
        ),
        notes=(
            "Config-patch application event (docs/design-config-and-"
            "dashboard.md §8).  subject_class=\"self\" because the system is "
            "the subject of the mutation; the operator's action is the "
            "upstream operator_action event referenced via "
            "operator_action_event_id.  Tier-B keys only -- numeric "
            "thresholds; no free text, no PII."
        ),
    ),

    # Invariant #1 audit gap (PR #144 review P0): the ASR transcript drives
    # _detect_explicit_remember and the addressing classifier, so it must
    # be emitted as an Event with provenance.  payload_kind="transcript"
    # per Event.payload_kind alphabet (schemas.py:100).  subject_class
    # defaults to "self" for v0.1f single-user manual test; multi-party
    # diarization (see roadmap-v0.1f-draft.md OQ-multiparty) will raise
    # this to "third_party" or "mixed".  transcript_text is wrapped in
    # SensitiveField at emission time for redaction routing.
    "asr_transcript_emitted": ToolEventSchema(
        payload_kind="transcript",
        subject_class="self",
        sensitivity="sensitive",
        retention_policy_id="transcript_audit_30d",
        required_fields=("transcript_text", "signal_event_id", "asr_model_label"),
        notes=(
            "Audit event for the ASR transcript that drives addressing + "
            "_detect_explicit_remember decisions.  transcript_text is wrapped "
            "in SensitiveField on the payload (free-text → redaction routing). "
            "caused_by[] closes through the TurnSignal evidence event that "
            "triggered the EOU.  Multi-party speaker diarization is out of "
            "scope for v0.1f; subject_class=\"self\" is the single-user lean."
        ),
    ),
}
