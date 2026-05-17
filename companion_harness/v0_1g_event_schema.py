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
    required_fields:     tuple[str, ...] = ()


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

    # v0.1j Task 8 invariant fix: emitted from MiniCPMStreamingModel._process_chunk
    # each time is_listen is updated.  event_id becomes the evidence_event_ids[0]
    # for the TurnSignal returned by MiniCPMNativeDuplexEouSource — closes the DAG.
    "native_duplex_invocation": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        notes=(
            "Emitted once per streaming_generate call in MiniCPMStreamingModel. "
            "Payload carries is_listen (bool) and ts_mono_ms.  Its event_id is "
            "stored as _last_native_duplex_event_id and surfaced via "
            "TurnSignal.evidence_event_ids[0] — this closes the causal DAG "
            "for native_duplex EOU signals (invariants #1 and #5)."
        ),
    ),

    # W-PR182-A: addressing classifier result — emitted post-ASR, pre-policy
    # for invariant #1 audit and Eval Phase C live-examiner gating.
    "addressing_classified": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=("classifier_name", "addressed", "confidence"),
        notes=(
            "Emitted once per addressing classification call (both primary "
            "MiniCPM and fallback WakeWord paths).  Payload carries "
            "classifier_name, addressed (bool), confidence (float), and "
            "evidence (str).  caused_by[] closes through the "
            "asr_transcript_emitted event_id when one was emitted, otherwise "
            "through the TurnSignal evidence event."
        ),
    ),

    # v0.2b T1: per-chunk diarization result (non-trivial, non-muted frames).
    # Causal chain: caused_by=[raw_audio_chunk.event_id].
    # Payload: speaker_id, confidence, is_new_speaker, model_revision.
    "diarization_frame_produced": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=("speaker_id", "confidence", "is_new_speaker", "model_revision"),
        notes=(
            "Emitted once per audio chunk that produces a non-trivial diarization "
            "frame (speaker_id is not None, muted=False).  Muted-window chunks and "
            "unvoiced chunks do NOT emit.  caused_by[] closes through the "
            "raw_audio_chunk event_id (invariant #1)."
        ),
    ),

    # v0.2b T1: once per wake-word confirmation when diarization is active.
    # Payload: speaker_id, wake_word_event_id.
    # caused_by=[<wake-word AddressingSignal event id>].
    "speaker_continuity_anchor": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=("speaker_id", "wake_word_event_id"),
        notes=(
            "Emitted once per wake-word confirmation when the diarization adapter "
            "has a non-null speaker_id for the same chunk.  Provides the durable "
            "anchor consumed by the v0.1k speaker-continuity tie-breaker in "
            "derive_user_addressed_agent (Anchor 7).  Idempotent within a "
            "wake-word episode."
        ),
    ),

    # F4 fix: emitted by _synthesis_dispatch_task when the proposal batch
    # window expires before any ThinkerProposal arrives.  Carries the
    # dispatcher state, batch-window sizing, and the TurnSignal event id so
    # operators can determine WHICH turn timed out and HOW long the window was.
    "synthesis_skipped_no_proposal": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=(
            "dispatcher_state", "batch_window_ms",
            "batch_open_at_ms", "batch_close_at_ms", "signal_evt_id",
        ),
        notes=(
            "Emitted when the synthesis dispatcher's grace window expires "
            "before any ThinkerProposal arrives.  payload_inline carries "
            "dispatcher_state, batch_window_ms, batch_open_at_ms, "
            "batch_close_at_ms, and signal_evt_id so operators can triage "
            "F0c-class proposer-timing failures without source spelunking."
        ),
    ),

    # logprob classifier: ambivalent result (prob_yes in [0.45, 0.55]).
    # Payload: confidence (float), transcript_preview (str, 80-char truncation),
    # classifier (str).  Sensitivity is sensitive (contains user speech preview).
    "addressing_classifier_low_confidence": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="sensitive",
        retention_policy_id="signal_default_30d",
        required_fields=("confidence", "transcript_preview", "classifier"),
        notes=(
            "Emitted when classify_yes_no() returns prob_yes in [0.45, 0.55] — "
            "model is ambivalent.  Lets operators spot prompt-design problems. "
            "transcript_preview is the first 80 chars of user speech (SensitiveField)."
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

    # Path B (§3.3): one per ring-append from MiniCPMStreamingModel.
    # High-rate (potentially 1 Hz continuous); default-OFF in dashboard filter
    # (matches PR #321 pattern). payload_inline carries only a 32-char preview
    # per §3.6 option (b) — no full text. payload_ref is always None unless
    # --audit-speculations is set (§3.6, deferred to PR 7).
    "proposer_token_buffered": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=("ring_seq", "is_listen", "text_preview"),
        notes=(
            "Emitted once per ring-append in Path B (flag ON). "
            "ring_seq is the monotonic index; text_preview is truncated "
            "to 32 chars. Full text is NOT stored by default (§3.6 option b). "
            "High-rate — add to default-OFF dashboard filter list."
        ),
    ),

    # AudioOutBroker per-subscriber queue overflow (invariant #10).
    # Throttled to at most 1 per subscriber per 60s.
    # Default-OFF in dashboard filter (low-signal for normal operation).
    "audio_subscriber_drop": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=("subscriber_id", "subscriber_drop_count", "queue_depth", "session_id", "seq"),
        notes=(
            "Emitted by AudioOutBroker when a per-listener audio_out queue overflows "
            "and a chunk is dropped (invariant #10). Throttled: at most 1 per "
            "subscriber per 60s to prevent feedback-loop amplification. "
            "Mirrors display_subscriber_drop (PR #321)."
        ),
    ),

    # Path B (§3.3): one per policy decision in Path B (silence or full_response).
    # caused_by: [signal_evt_id, policy_evt_id].
    "commit_or_discard": StageSixEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=(
            "committed", "discarded_token_count", "committed_token_count",
            "signal_evt_id", "policy_evt_id",
        ),
        notes=(
            "Emitted once per EOU policy decision in Path B. "
            "committed=True means ring tail was snapshotted + dispatched to Kokoro; "
            "committed=False means ring tail was discarded. "
            "discarded_token_count / committed_token_count are integer counts only "
            "(no full text, per §3.6 option b)."
        ),
    ),
}
