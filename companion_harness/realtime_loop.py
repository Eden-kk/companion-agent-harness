"""RealtimeOrchestrator — live-loop pump (live-loop Task 5).

Continuously pumps: InputIngest audio → TurnDetectorSuite → SpeakPolicy.decide()
→ (if action ≠ silence) ForegroundModel.process_stream() → AudioOutputController
→ TtsAdapter sink.

Gating contract (hard invariant — invariants #2, #4):
  SpeakPolicy.decide() is called EXACTLY ONCE per candidate batch BEFORE any
  synthesis byte is produced.  The orchestrator collects all ThinkerProposal
  candidates from the foreground model, THEN calls decide(), THEN (only if the
  action_type is a speech action) calls TtsAdapter.synthesize().  No synthesis
  byte is ever produced without a prior approved SpeakDecision.

EventLogger discipline (invariant #10):
  The realtime path calls only the non-blocking EventLogger.log().  The drain
  loop is an independent asyncio Task started by the caller via EventLogger.start().
  The orchestrator never awaits logger queue draining.

Causal closure (invariant #1):
  Every Event emitted by this module carries caused_by[] pointing at its trigger.
  The DAG closes from harness_init through assistant_audio_buffer_flushed.

Determinism boundary (invariant #5):
  Wall-clock and jitter live OUTSIDE SpeakPolicy.  The orchestrator builds
  PolicyInputs from TurnSignal fields only — no wall-clock reads leak in.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Callable

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest, IngestSession
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.tts_adapter import TtsAdapter

__all__ = ["RealtimeOrchestrator"]

_SCHEMA_VERSION = "0.1"
_SOURCE = "realtime_orchestrator"


class RealtimeOrchestrator:
    """Pumps the live loop for one session.

    Inject all adapters at construction; call run() with an async iterable of
    (pcm_bytes, CaptureMetadata) pairs.  run() drives the full pipeline and
    returns when the iterable is exhausted.

    The EventLogger drain loop must be started (EventLogger.start()) by the
    caller before run() is called (invariant #10 — drain is an independent task).
    """

    def __init__(
        self,
        ingest: InputIngest,
        detectors: list[object],  # objects with process_frame(frame, caused_by) -> TurnSignal | None
        speak_policy: Callable[[PolicyInputs, list[str], float], SpeakDecision],
        foreground: ForegroundModel,
        controller: AudioOutputController,
        tts: TtsAdapter,
        logger: EventLogger,
        session_id: str,
    ) -> None:
        self._ingest = ingest
        self._detectors = detectors
        self._speak_policy = speak_policy
        self._foreground = foreground
        self._controller = controller
        self._tts = tts
        self._logger = logger
        self._session_id = session_id
        self._seq = 0

    async def run(
        self,
        session: IngestSession,
        chunks: AsyncIterator[tuple[bytes, CaptureMetadata]],
    ) -> None:
        """Drive the live loop until the chunk stream is exhausted."""
        async for pcm_bytes, meta in chunks:
            chunk_event = self._ingest.ingest_chunk(session, pcm_bytes, meta)
            caused_by = [chunk_event.event_id]

            # Fan chunk to all detectors; collect TurnSignals
            signals: list[TurnSignal] = []
            for detector in self._detectors:
                sig = detector.process_frame(pcm_bytes, caused_by)  # type: ignore[attr-defined]
                if sig is not None:
                    signals.append(sig)

            if not signals:
                continue

            # Aggregate signal evidence event IDs for causal closure
            signal_event_ids: list[str] = []
            for sig in signals:
                signal_event_ids.extend(sig.evidence_event_ids)
            if not signal_event_ids:
                signal_event_ids = caused_by

            # Build PolicyInputs from TurnSignals (no wall-clock — invariant #5)
            policy_inputs = _signals_to_policy_inputs(signals)

            # p_backchannel: max across all signals
            p_backchannel = max(sig.p_backchannel for sig in signals)

            # GATING CONTRACT (invariants #2, #4):
            # Collect ALL foreground proposals BEFORE calling decide().
            # No synthesis byte may be produced before this decision returns.
            proposals: list[ThinkerProposal] = []
            async for proposal in self._foreground.process_stream(
                _single_frame_iter(pcm_bytes),
                caused_by=signal_event_ids,
            ):
                proposals.append(proposal)

            # SpeakPolicy.decide() called EXACTLY ONCE per candidate batch,
            # AFTER all proposals are collected, BEFORE any synthesis byte.
            decision = self._speak_policy(policy_inputs, signal_event_ids, p_backchannel)

            # Log the speak_decision event (causal closure — invariant #1)
            decision_event_id = self._emit_speak_decision(decision, signal_event_ids)

            if decision.action_type == "silence":
                continue

            # Only reach synthesis AFTER an approved SpeakDecision (invariants #2, #4).
            gen_event_id = self._controller.start_generation(caused_by=[decision_event_id])

            # Select best proposal text (first by confidence, fallback to empty)
            text = _best_proposal_text(proposals)

            audio_chunks = self._tts.synthesize(text, decision.allowed_prosody_tags)
            await self._controller.play(audio_chunks, generation_event_id=gen_event_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_speak_decision(self, decision: SpeakDecision, caused_by: list[str]) -> str:
        """Log a speak_decision event and return its event_id."""
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-orch-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"speak_decision:{event_id}:{decision.action_type}".encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=seq,
            event_type="speak_decision",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="default",
        )
        self._logger.log(evt)
        return event_id


# ---------------------------------------------------------------------------
# Module-level helpers (no state)
# ---------------------------------------------------------------------------


def _signals_to_policy_inputs(signals: list[TurnSignal]) -> PolicyInputs:
    """Build PolicyInputs from aggregated TurnSignals.

    No wall-clock reads — all values derive from signal fields (invariant #5).
    eou_probability = max p_done across signals (most confident detector wins).
    user_speaking = True when no signal has p_done > p_continue.
    """
    max_p_done = max(sig.p_done for sig in signals)
    max_p_continue = max(sig.p_continue for sig in signals)
    user_speaking = max_p_done <= max_p_continue

    return PolicyInputs(
        user_speaking=user_speaking,
        eou_probability=max_p_done,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="default",
        current_task_mode="default",
        social_mode="default",
        risk_mode="default",
        cooldown_state={},
        attachment_risk_level=0.0,
        audio_visual_conflict_score=0.0,
        grounding_confidence=1.0,
        deictic_ambiguous=False,
    )


async def _single_frame_iter(
    pcm_bytes: bytes,
) -> AsyncIterator[tuple[bytes, bytes | None]]:
    """Yield one (audio, None) frame pair, for single-chunk foreground invocations."""
    yield pcm_bytes, None


def _best_proposal_text(proposals: list[ThinkerProposal]) -> str:
    if not proposals:
        return ""
    return max(proposals, key=lambda p: p.confidence).content
