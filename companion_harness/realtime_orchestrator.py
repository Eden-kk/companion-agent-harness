"""StreamingRealtimeOrchestrator — async task-graph live-loop pump (live-loop Task 5b/6).

Pumps: audio_in Queue → _audio_tee_task → T1 (detectors) → T2 (policy gate)
       → T3 (foreground stream) → T4 (synthesis dispatch).

Gating contract (hard invariant — invariants #2, #4):
  SpeakPolicy.decide() is called EXACTLY ONCE per candidate batch BEFORE any
  synthesis byte is produced.  An asyncio.Future[SpeakDecision] per batch is
  the gating primitive; T4 awaits the Future before touching TtsAdapter.

EventLogger discipline (invariant #10):
  Only EventLogger.log() (non-blocking) is called on the hot path.
  await logger.stop() is called only inside stop() (cold path).

Causal closure (invariant #1):
  Every Event carries caused_by[] pointing at its trigger.

Determinism boundary (invariant #5):
  signal_history is sorted by evidence_event_ids[0] lexicographic BEFORE
  crossing into policy_inputs_builder.  No wall-clock or jitter enters PolicyInputs.

Barge-in (Task 6):
  T2 also receives _VadOnsetFrame items (p_speech + vad_frame event_id) from T1.
  When onset predicate fires during playback, T2 emits vad_user_speech_onset and
  spawns _fire_barge_in as a background task. _fire_barge_in calls request_stop()
  then hard-cancels via cancel_generation() if play_task does not finish within
  hard_cancel_after_ms. New event types: vad_user_speech_onset, barge_in_trigger_no_op.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from companion_harness.addressing_classifier import (
    AddressingClassifier,
    MiniCPMAddressingClassifier,
    derive_user_addressed_agent,
)
from companion_harness.asr_adapter import ASRModel
from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.decision_trace_store import DecisionTraceStore
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import IngestSession
from companion_harness.native_duplex_eou import NativeDuplexEouSource, _NullNativeDuplexEouSource
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    Event,
    MemoryItem,
    PolicyInputs,
    SensitiveField,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.speak_policy import build_decision_trace as _build_decision_trace
from companion_harness.speak_policy import decide as _default_speak_policy_decide
from companion_harness.tool_router import ToolDispatchRequest
from companion_harness.tts_adapter import TtsAdapter
from companion_harness.user_reduction_commands import (
    apply_user_reduction_command,
    detect_user_reduction_command,
    make_user_reduction_payload,
)
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector

if TYPE_CHECKING:
    from companion_harness.deictic_detector import DeicticDetector
    from companion_harness.memory_manager import MemoryManager
    from companion_harness.vision_sidecar import VisionSidecar
    from manual_test_console.config_store import ConfigStore

__all__ = ["StreamingRealtimeOrchestrator"]

_SCHEMA_VERSION = "0.1"

_SOURCE = "streaming_realtime_orchestrator"

RETRIEVAL_TOP_K = 5
MEMORY_EVENT_PAYLOADS_CAP = 1024


def _detect_explicit_remember(transcript: str) -> tuple[bool, str | None]:
    """Detect explicit 'remember' / 'don't forget' intent in user transcript.

    Returns (matched, extracted_content). v0.1e whitelist:
    - "remember that ..."
    - "please remember ..."
    - "don't forget ..."
    ("note that" intentionally excluded — too conversational, false-positive risk.)
    """
    text = transcript.lower().strip()
    for phrase in ("remember that ", "please remember ", "don't forget "):
        if text.startswith(phrase):
            return True, transcript[len(phrase):].strip()
    return False, None


def _detect_explicit_forget(transcript: str) -> tuple[bool, str | None]:
    """Detect explicit 'forget' intent in user transcript.

    Returns (matched, query) where query is the substring identifying the
    target memory item(s). Whitelist:
    - "forget that ..."
    - "forget about ..."
    - "please forget ..."
    Bare "forget that" / "forget about" with no trailing content is rejected to
    avoid false positives (query would be empty → tombstone every item).
    """
    text = transcript.lower().strip()
    for phrase in ("forget that ", "forget about ", "please forget "):
        if text.startswith(phrase):
            extracted = transcript[len(phrase):].strip()
            if extracted:
                return True, extracted
    return False, None


@dataclass
class _VadOnsetFrame:
    """Lightweight internal item sent from T1 to T2's inbox for onset detection."""
    p_speech: float
    frame_event_id: str

_AUDIO_TEE_DEPTH = 256  # raised from 64; 2.56 s headroom at 100 frames/s
_AUDIO_QUEUE_BOUND = 64
_TURN_SIGNAL_QUEUE_BOUND = 32
_POLICY_DECISIONS_QUEUE_BOUND = 8
_AUDIO_TEE_DEPTH_MIN = 32
_AUDIO_TEE_DEPTH_MAX = 1024


def _drop_oldest_put(
    q: asyncio.Queue[Any],
    item: Any,
    logger: EventLogger,
    source_event_id: str,
    tee_name: str,
) -> bool:
    """Put item onto a bounded audio queue; drop oldest on overflow + log.

    Returns True when a drop occurred so callers can tally per-tee drop counts.
    The ``source_event_id`` is the upstream chunk's real event_id; it replaces
    the sentinel string ``"_dropped_before_enqueue"`` so the causal DAG closes.
    """
    try:
        q.put_nowait(item)
        return False
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        q.put_nowait(item)
        logger.log(_make_degrade_event([source_event_id], tee_name, q.qsize()))
        return True


def _make_degrade_event(
    caused_by: list[str],
    tee_name: str = "",
    current_queue_depth: int = 0,
) -> Event:
    now_ms = int(time.monotonic() * 1000)
    inline: dict | None = None
    if tee_name:
        inline = {"tee_name": tee_name, "current_queue_depth": current_queue_depth}
    return Event(
        event_id=f"degrade-{now_ms}-{uuid.uuid4().hex[:8]}",
        session_id="",
        schema_version=_SCHEMA_VERSION,
        seq_no=0,
        event_type="log_drop_or_degrade",
        timestamp_mono_ms=now_ms,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source=_SOURCE,
        caused_by=caused_by,
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="default",
        payload_inline=inline,
    )


class StreamingRealtimeOrchestrator:
    """Continuously pumps the live loop via four asyncio tasks + one audio-tee task.

    Adapters injected at construction; no SDK imports.  The caller pushes
    (frame_bytes, raw_audio_chunk_event_id) tuples onto audio_in and
    the orchestrator drives the full pipeline.

    Task graph:
      _audio_tee_task   : audio_in → tee_to_detectors, tee_to_foreground
      T1 _detector_fanout_task   : tee_to_detectors → turn_signals
      T2 _policy_gate_task       : turn_signals → policy_decisions
      T3 _foreground_stream_task : tee_to_foreground → proposal_buffer
      T4 _synthesis_dispatch_task: policy_decisions + proposal_buffer → AudioOutputController

    Runtime config (Tier B, per docs/design-config-and-dashboard.md §3):
      When ``config_store`` is provided, the 12 Tier-B keys are read once per
      ``_policy_gate_task`` iteration BEFORE ``policy_inputs_builder`` runs.
      Orchestrator-level keys (proposal_batch_window_ms, hard_cancel_after_ms,
      p_speech_thresh, p_backchannel_thresh) update self state. Detector keys
      (vad / smart_turn / backchannel) are applied via setters at the EOU
      boundary. Policy keys (backchannel / audio_visual_conflict /
      grounding_confidence) are passed as kwargs into the speak_policy
      callable. Policy thresholds apply within the current decision; detector
      thresholds apply starting on the next frame after the EOU.
    """

    SOURCE = _SOURCE
    SCHEMA_VERSION = _SCHEMA_VERSION

    def __init__(
        self,
        *,
        session_id: str,
        logger: EventLogger,
        ingest_session: IngestSession,
        audio_in: asyncio.Queue[tuple[bytes, str]],
        vad_detector: VADDetector,
        smart_turn_detector: SmartTurnDetector,
        backchannel_classifier: BackchannelClassifier,
        policy_inputs_builder: Callable[[TurnSignal, list[TurnSignal]], PolicyInputs],
        speak_policy: Callable[[PolicyInputs, list[str], float], SpeakDecision] | None = None,
        foreground_model: ForegroundModel,
        audio_output: AudioOutputController,
        tts_adapter: TtsAdapter,
        proposal_batch_window_ms: int = 80,
        hard_cancel_after_ms: int = 120,
        p_speech_thresh: float = 0.5,
        p_backchannel_thresh: float = 0.7,
        decision_trace_dir: Path | None = None,
        episodic_store: "MemoryManager | None" = None,
        semantic_store: "MemoryManager | None" = None,
        asr_model: ASRModel | None = None,
        vision_sidecar: "VisionSidecar | None" = None,
        addressing_classifier: AddressingClassifier | None = None,
        minicpm_addressing_classifier: MiniCPMAddressingClassifier | None = None,
        config_store: "ConfigStore | None" = None,
        native_duplex_eou_source: NativeDuplexEouSource | None = None,
        deictic_detector: "DeicticDetector | None" = None,
        tool_router: "Any | None" = None,
        tool_progress_emitter: "Any | None" = None,
        background_reasoner: "Any | None" = None,
        audio_tee_depth: int = _AUDIO_TEE_DEPTH,
    ) -> None:
        self._session_id = session_id
        self._logger = logger
        self._ingest_session = ingest_session
        self._audio_in = audio_in
        self._vad_detector = vad_detector
        self._smart_turn_detector = smart_turn_detector
        self._backchannel_classifier = backchannel_classifier
        self._policy_inputs_builder = policy_inputs_builder
        self._speak_policy_fn = speak_policy if speak_policy is not None else _default_speak_policy_decide
        self._foreground_model = foreground_model
        self._audio_output = audio_output
        self._tts_adapter = tts_adapter
        # Empirical calibration required on b200 once MiniCPM-o first-token latency is measured.
        # If first-token consistently exceeds this, edge case (d) fires every batch.
        self._proposal_batch_window_ms = proposal_batch_window_ms
        self._hard_cancel_after_ms = hard_cancel_after_ms
        self._p_speech_thresh = p_speech_thresh
        self._p_backchannel_thresh = p_backchannel_thresh

        # Internal queues
        _tee_depth = max(_AUDIO_TEE_DEPTH_MIN, min(_AUDIO_TEE_DEPTH_MAX, audio_tee_depth))
        self._tee_depth = _tee_depth
        self._tee_to_detectors: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=_tee_depth)
        self._tee_to_foreground: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=_tee_depth)
        # Per-tee drop counters for the 60s summary event.
        self._tee_detectors_drop_count: int = 0
        self._tee_foreground_drop_count: int = 0
        self._tee_summary_last_ms: float = time.monotonic() * 1000

        # T2 inbox: union of _VadOnsetFrame (for onset detection) and TurnSignal tuples (for EOU decisions)
        self._t2_inbox: asyncio.Queue[_VadOnsetFrame | tuple[TurnSignal, str]] = asyncio.Queue(
            maxsize=_AUDIO_QUEUE_BOUND + _TURN_SIGNAL_QUEUE_BOUND
        )
        # 3-tuple: (signal_evt_id, Future[SpeakDecision], policy_decision_event_id)
        self._policy_decisions: asyncio.Queue[tuple[str, asyncio.Future[SpeakDecision], str]] = asyncio.Queue(
            maxsize=_POLICY_DECISIONS_QUEUE_BOUND
        )

        # Shared state for gating (single-writer T3 / single-reader-and-clear T4)
        self.proposal_buffer: list[ThinkerProposal] = []
        self._first_proposal_event = asyncio.Event()
        self._batch_open_event = asyncio.Event()
        self._batch_close_event = asyncio.Event()

        # Coalescing guard — T2 only
        # _decision_in_flight is True from the moment a TurnSignal passes the guard
        # until the resulting (signal_evt_id, future, policy_evt_id) tuple has been
        # put onto _policy_decisions.  It is NOT cleared when the future becomes done
        # (set_result is called before the put), which was the pre-fix bug: a done
        # future made the old guard evaluate to False, letting subsequent same-turn
        # signals through as duplicate decisions.
        self._decision_in_flight: bool = False
        self._pending_decision_future: asyncio.Future[SpeakDecision] | None = None
        self._pending_signal_evt_id: str = ""

        # Signal history for determinism boundary (sorted before crossing into policy_inputs_builder)
        self._signal_history: list[TurnSignal] = []

        # Barge-in state — T2 only (Task 6)
        self._speech_onset_debounce_active: bool = False
        self._latest_p_backchannel: float = 0.0
        self._barge_in_in_flight: bool = False
        self._barge_in_tasks: list[asyncio.Task[None]] = []

        # In-flight tool call tracking (Task 7): set by T4 when dispatch starts,
        # cleared on completion or cancellation.
        self._inflight_tool_call_id: str | None = None

        # decision_trace_dir defaults to a subdir of the process cwd when not provided.
        # Callers that care about the location should pass it explicitly.
        _trace_dir = decision_trace_dir if decision_trace_dir is not None else Path("decision_traces")
        self._decision_trace_store = DecisionTraceStore(_trace_dir)

        self._episodic_store = episodic_store
        self._semantic_store = semantic_store
        self._asr_model = asr_model
        self._vision_sidecar = vision_sidecar
        # WakeWordAddressingClassifier is the safety-net; MiniCPM-derived is the primary.
        # UNAVAILABLE: #157 — libcudart blocker, MiniCPM-derived addressing unavailable.
        self._addressing_classifier = addressing_classifier
        self._minicpm_addressing_classifier = minicpm_addressing_classifier
        self._config_store = config_store
        # UNAVAILABLE: #157 — libcudart blocker; _NullNativeDuplexEouSource used
        # by default so every EOU decision routes through the SmartTurn/VAD
        # fallback path while emitting signal_producer_fallback for replay.
        self._native_duplex_eou_source: NativeDuplexEouSource = (
            native_duplex_eou_source if native_duplex_eou_source is not None else _NullNativeDuplexEouSource()
        )
        self._deictic_detector = deictic_detector
        self._tool_router = tool_router
        self._tool_progress_emitter = tool_progress_emitter
        self._background_reasoner = background_reasoner
        # Asyncio queue for smart-path requests (OQ-12). Unbounded: smart-path
        # calls are rare (one per tool dispatch decision with routing_tier="smart").
        self._smart_path_queue: asyncio.Queue[ToolDispatchRequest] = asyncio.Queue()
        # Last-applied policy thresholds (read from config_store at EOU); when
        # config_store is None we fall back to speak_policy.decide()'s defaults
        # by leaving these as None and not passing kwargs.
        self._policy_threshold_kwargs: dict[str, float] = {}
        self._pending_retrieved_items: list[MemoryItem] = []
        # Per-turn audio buffer (PCM16 bytes). Appended on every audio frame in
        # _audio_tee_task; consumed + cleared in T2 on EOU when asr_model is set.
        # No-op (always empty / never consumed) when asr_model is None.
        self._turn_audio_buffer: bytearray = bytearray()
        # FIFO-capped payload dict for memory_write_candidate and memory_retrieval_event.
        # Keyed by event_id; consumers access via payload_reader callback.
        self._memory_event_payloads: OrderedDict[str, dict] = OrderedDict()

        self._seq = 0
        self._tasks: list[asyncio.Task[None]] = []
        self._started_event_id: str = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Emit orchestrator_started and spawn the four tasks + tee task."""
        self._started_event_id = self._emit(
            "orchestrator_started",
            [self._ingest_session.session_open_id],
            "signal",
        ).event_id

        loop = asyncio.get_running_loop()
        self._tasks = [
            loop.create_task(self._audio_tee_task(), name="audio_tee"),
            loop.create_task(self._detector_fanout_task(), name="T1_detector_fanout"),
            loop.create_task(self._policy_gate_task(), name="T2_policy_gate"),
            loop.create_task(self._foreground_stream_task(), name="T3_foreground_stream"),
            loop.create_task(self._synthesis_dispatch_task(), name="T4_synthesis_dispatch"),
        ]
        if self._background_reasoner is not None:
            self._tasks.append(
                loop.create_task(self._smart_path_task(), name="T5_smart_path")
            )

    async def stop(self) -> None:
        """Cancel all tasks (including barge-in watchdogs), emit orchestrator_stopped, drain logger."""
        for task in self._tasks:
            task.cancel()
        for task in self._barge_in_tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in self._barge_in_tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._barge_in_tasks.clear()

        self._emit(
            "orchestrator_stopped",
            [self._started_event_id] if self._started_event_id else [self._ingest_session.session_open_id],
            "signal",
        )
        await self._logger.stop()

    async def run(self) -> None:
        """Start all tasks and block until stopped via stop()."""
        await self.start()
        try:
            await asyncio.gather(*self._tasks)
        except (asyncio.CancelledError, Exception):
            pass

    def dispatch_smart(self, request: ToolDispatchRequest) -> None:
        """Enqueue a smart-path tool dispatch request (non-blocking, invariant #10).

        No-op when background_reasoner is None (fast-path / no smart path wired).
        """
        if self._background_reasoner is not None:
            self._smart_path_queue.put_nowait(request)

    # ------------------------------------------------------------------
    # Internal tasks
    # ------------------------------------------------------------------

    async def _audio_tee_task(self) -> None:
        """Read audio_in; fan to tee_to_detectors and tee_to_foreground (drop-oldest)."""
        _SUMMARY_INTERVAL_MS = 60_000
        while True:
            frame_bytes, chunk_event_id = await self._audio_in.get()
            item = (frame_bytes, chunk_event_id)
            if _drop_oldest_put(
                self._tee_to_detectors, item, self._logger, chunk_event_id, "detectors"
            ):
                self._tee_detectors_drop_count += 1
            if _drop_oldest_put(
                self._tee_to_foreground, item, self._logger, chunk_event_id, "foreground"
            ):
                self._tee_foreground_drop_count += 1
            # Accumulate per-turn audio for ASR (consumed + cleared in T2 on EOU).
            # Skip when no ASR is wired so the buffer never grows unbounded.
            if self._asr_model is not None:
                self._turn_audio_buffer.extend(frame_bytes)
            # Periodic 60s drop summary for operator observability.
            now_ms = time.monotonic() * 1000
            if now_ms - self._tee_summary_last_ms >= _SUMMARY_INTERVAL_MS:
                self._tee_summary_last_ms = now_ms
                for tee_name, drop_count, q in (
                    ("detectors", self._tee_detectors_drop_count, self._tee_to_detectors),
                    ("foreground", self._tee_foreground_drop_count, self._tee_to_foreground),
                ):
                    self._logger.log(self._emit_tee_summary(tee_name, drop_count, q.qsize()))
                self._tee_detectors_drop_count = 0
                self._tee_foreground_drop_count = 0

    def _emit_tee_summary(self, tee_name: str, drop_count_60s: int, current_queue_depth: int) -> Event:
        now_ms = int(time.monotonic() * 1000)
        return Event(
            event_id=f"tee-summary-{now_ms}-{uuid.uuid4().hex[:8]}",
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=0,
            event_type="audio_tee_drop_summary",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=[self._started_event_id] if self._started_event_id else [],
            payload_hash="",
            payload_ref=None,
            payload_kind="signal",
            subject_class="unknown",
            sensitivity="safe",
            retention_policy_id="default",
            payload_inline={
                "drop_count_60s": drop_count_60s,
                "tee_name": tee_name,
                "current_queue_depth": current_queue_depth,
            },
        )

    async def _detector_fanout_task(self) -> None:
        """T1: Fan audio frames to all detectors; forward TurnSignals and VAD onset frames to T2.

        SmartTurn veto (Finding 13): when SmartTurn fires with p_continue > p_done
        (thinking-pause window), its signal is authoritative and any VAD signal from
        the same frame is suppressed.  Both detectors are still called every frame so
        their internal buffers advance correctly; the veto only affects what is placed
        on _t2_inbox.
        """
        while True:
            frame_bytes, chunk_event_id = await self._tee_to_detectors.get()
            caused_by = [chunk_event_id]

            vad_sig: TurnSignal | None = self._vad_detector.process_frame(frame_bytes, caused_by)
            smart_sig: TurnSignal | None = self._smart_turn_detector.process_frame(frame_bytes, caused_by)
            bc_sig: TurnSignal | None = self._backchannel_classifier.process_frame(frame_bytes, caused_by)

            # SmartTurn veto: when SmartTurn fires p_continue > p_done (thinking pause),
            # suppress both the VAD signal and the SmartTurn signal from reaching the EOU
            # gate.  The SmartTurn signal itself carries p_done < p_continue, which would
            # still trigger a premature policy decision if forwarded.  Log the suppression
            # for audit (invariant #1) and wait for the next silence candidate — at which
            # point SmartTurn may return p_done > p_continue (real end-of-turn).
            smart_turn_vetoes_eou = (
                smart_sig is not None and smart_sig.p_continue > smart_sig.p_done
            )

            if smart_turn_vetoes_eou:
                # Log suppression events for both signals that are being held back.
                suppressed_evt_id = (
                    smart_sig.evidence_event_ids[0]
                    if smart_sig.evidence_event_ids
                    else chunk_event_id
                )
                self._emit(
                    "vad_signal_suppressed_by_smart_turn",
                    [suppressed_evt_id],
                    "signal",
                )
            else:
                for sig in (vad_sig, smart_sig, bc_sig):
                    if sig is None:
                        continue
                    signal_evt_id = sig.evidence_event_ids[0] if sig.evidence_event_ids else chunk_event_id
                    await self._t2_inbox.put((sig, signal_evt_id))

            # Forward VAD frame onset info to T2 for barge-in detection (Task 6).
            await self._t2_inbox.put(
                _VadOnsetFrame(
                    p_speech=self._vad_detector.last_frame_p_speech,
                    frame_event_id=self._vad_detector.last_frame_event_id,
                )
            )

    async def _policy_gate_task(self) -> None:
        """T2: Handle VAD onset frames (barge-in) and EOU TurnSignals (policy gate)."""
        while True:
            item = await self._t2_inbox.get()

            # --- VAD onset frame: check barge-in predicate ---
            if isinstance(item, _VadOnsetFrame):
                if self._should_emit_speech_onset(item):
                    onset_evt = self._make_event(
                        event_id=self._new_event_id(),
                        event_type="vad_user_speech_onset",
                        caused_by=[item.frame_event_id],
                        payload_kind="signal",
                    )
                    self._logger.log(onset_evt)
                    self._speech_onset_debounce_active = True
                    if self.is_barge_in_trigger():
                        # Set flag synchronously BEFORE create_task (BLOCKER 3).
                        self._barge_in_in_flight = True
                        task = asyncio.get_running_loop().create_task(
                            self._fire_barge_in(onset_evt.event_id),
                            name="barge_in_watchdog",
                        )
                        self._barge_in_tasks.append(task)
                elif not self._audio_output.is_playing:
                    # Clear debounce when playback stops.
                    self._speech_onset_debounce_active = False
                continue  # VAD frames do not feed the EOU decision path

            # --- TurnSignal: EOU policy decision path ---
            signal, signal_evt_id = item

            # --- EOU producer routing (Anchor 3, v0.1j Task 8) ---
            # native_duplex is the spec-named primary EOU source. When it returns
            # a signal, use it directly. When it returns None (unavailable or below
            # threshold), keep the SmartTurn/VAD signal that arrived here and emit
            # signal_producer_fallback so replay knows which path fired.
            # UNAVAILABLE: #157 — libcudart blocker; native_duplex EOU unavailable.
            native_signal = self._native_duplex_eou_source.get_eou_signal()
            if native_signal is not None:
                signal = native_signal
                signal_evt_id = (
                    native_signal.evidence_event_ids[0] if native_signal.evidence_event_ids else signal_evt_id
                )
            else:
                fallback_evt = self._make_event(
                    event_id=self._new_event_id(),
                    event_type="signal_producer_fallback",
                    caused_by=[signal_evt_id],
                    payload_kind="signal",
                    extra_hash="native_duplex->smart_turn",
                )
                fallback_evt = dataclasses.replace(
                    fallback_evt,
                    payload_inline={
                        "primary_producer": "native_duplex",
                        "fallback_producer": signal.detector,
                        "reason": "native_duplex_unavailable",
                    },
                )
                self._logger.log(fallback_evt)

            # Track latest backchannel score for barge-in post-validation.
            self._latest_p_backchannel = signal.p_backchannel

            # Coalescing guard (edge case i): skip if a decision is already in flight.
            # _decision_in_flight remains True until after await _policy_decisions.put(),
            # so it stays set even after set_result() marks the future done — that was
            # the pre-fix bug (done future → guard evaluated False → duplicate decision).
            if self._decision_in_flight:
                evt_type = (
                    "coalesced_during_playback"
                    if self._audio_output.is_playing
                    else "turn_signal_coalesced"
                )
                self._emit(evt_type, [self._pending_signal_evt_id], "signal")
                continue

            self._decision_in_flight = True

            # --- ConfigStore snapshot (per docs/design-config-and-dashboard.md §3) ---
            # Read all 12 Tier-B keys once at the EOU boundary. There is no await
            # between this snapshot and _speak_policy_decide() below, so no
            # config_change can land mid-decision (determinism boundary). Detector
            # setter calls take effect starting on the next frame; policy threshold
            # kwargs apply within the current decision (asymmetry documented in
            # class docstring).
            self._snapshot_config()

            # Maintain signal history (for determinism boundary).
            self._signal_history.append(signal)
            # Sort by evidence_event_ids[0] lexicographic — determinism boundary (§6).
            sorted_history = sorted(
                self._signal_history,
                key=lambda s: s.evidence_event_ids[0] if s.evidence_event_ids else "",
            )

            inputs = self._policy_inputs_builder(signal, sorted_history)

            # --- ASR: transcribe the just-completed turn (synchronous in T2). ---
            # On EOU the buffered PCM16 audio is fed to whisper-tiny.en (~50–80ms
            # on b200). EOU is rare relative to the 32ms frame cadence so this
            # synchronous call is acceptable. Buffer is cleared immediately so
            # audio does not leak into the next turn. When asr_model is None,
            # transcript stays "" (backward compatible).
            transcript = ""
            if self._asr_model is not None:
                if self._turn_audio_buffer:
                    transcript = self._asr_model(bytes(self._turn_audio_buffer))
                self._turn_audio_buffer.clear()
            inputs.user_transcript = transcript

            # --- Invariant #1: emit the ASR transcript as an Event (PR #144 P0). ---
            # The transcript drives _detect_explicit_remember and the addressing
            # classifier; per invariant #1 ("no unlogged behavior") it must be
            # recorded with provenance. Skip emission on empty transcript (no
            # behavior driven → nothing to log) and when no ASR is wired.
            transcript_evt_id: str | None = None
            if self._asr_model is not None and transcript:
                transcript_evt_id = self._new_event_id()
                transcript_payload = {
                    "transcript_text": SensitiveField(
                        retention_policy_id="transcript_audit_30d",
                        value=transcript,
                        sensitivity="sensitive",
                        source_event_ids=[signal_evt_id],
                    ),
                    "signal_event_id": signal_evt_id,
                    "asr_model_label": getattr(self._asr_model, "label", "asr"),
                }
                self._store_payload(transcript_evt_id, transcript_payload)
                transcript_evt = dataclasses.replace(
                    self._make_event(
                        event_id=transcript_evt_id,
                        event_type="asr_transcript_emitted",
                        caused_by=[signal_evt_id],
                        payload_kind="transcript",
                    ),
                    subject_class="self",
                    sensitivity="sensitive",
                    retention_policy_id="transcript_audit_30d",
                    payload_ref=f"orchestrator://{transcript_evt_id}",
                )
                self._logger.log(transcript_evt)

            # --- Retrieval: fires on every EOU before decide() (plan §4.2 v4) ---
            stores_queried: list[str] = []
            retrieved_items: list[MemoryItem] = []
            if self._episodic_store is not None:
                retrieved_items.extend(self._episodic_store.retrieve(transcript, top_k=RETRIEVAL_TOP_K))
                stores_queried.append("episodic")
            if self._semantic_store is not None:
                retrieved_items.extend(self._semantic_store.retrieve(transcript, top_k=RETRIEVAL_TOP_K))
                stores_queried.append("semantic_relational")

            mre_event_id = self._new_event_id()
            mre_payload = {
                "query": transcript,
                "top_k": RETRIEVAL_TOP_K,
                "stores_queried": stores_queried,
                "result_item_ids": [it.item_id for it in retrieved_items],
                "result_count": len(retrieved_items),
                "privacy_mode": inputs.privacy_mode,
            }
            self._store_payload(mre_event_id, mre_payload)
            mre_event = dataclasses.replace(
                self._make_event(
                    event_id=mre_event_id,
                    event_type="memory_retrieval_event",
                    caused_by=[signal_evt_id],
                    payload_kind="memory_op",
                    extra_hash=str(len(retrieved_items)),
                ),
                subject_class="self",
                sensitivity="safe",
                retention_policy_id="retrieval_audit_30d",
                payload_ref=f"orchestrator://{mre_event_id}",
            )
            self._logger.log(mre_event)

            self._foreground_model.set_context(retrieved_items)
            self._pending_retrieved_items = retrieved_items
            inputs.retrieved_items = retrieved_items

            # --- Addressing: MiniCPM-derived primary; WakeWord safety-net ---
            # MiniCPM-derived classifier is the final-product primary (issue #139).
            # WakeWordAddressingClassifier is the safety-net, active when MiniCPM
            # returns None (unavailable — UNAVAILABLE: #157 libcudart blocker).
            # A signal_producer_fallback event is emitted whenever the safety-net fires.
            # W-PR182-A: addressing_classified is emitted for EVERY classification
            # call (primary and fallback) for invariant #1 audit.
            _addressing_caused_by = (
                [transcript_evt_id] if transcript_evt_id is not None else [signal_evt_id]
            )
            _confidence_float = {"explicit": 1.0, "implicit": 0.5, "background": 0.0}
            addressing_signal = None
            if self._minicpm_addressing_classifier is not None:
                addressing_signal = self._minicpm_addressing_classifier(
                    transcript=transcript,
                    speaker_count=None,
                    social_mode=inputs.social_mode,
                )
                if addressing_signal is not None:
                    _addr_evt = self._make_event(
                        event_id=self._new_event_id(),
                        event_type="addressing_classified",
                        caused_by=_addressing_caused_by,
                        payload_kind="signal",
                    )
                    self._logger.log(dataclasses.replace(
                        _addr_evt,
                        retention_policy_id="signal_default_30d",
                        payload_inline={
                            "classifier_name": type(self._minicpm_addressing_classifier).__name__,
                            "addressed": derive_user_addressed_agent(addressing_signal, inputs.social_mode, transcript),
                            "confidence": _confidence_float.get(addressing_signal.confidence, 0.5),
                            "evidence": addressing_signal.evidence,
                        },
                    ))
            if addressing_signal is None and self._addressing_classifier is not None:
                addressing_signal = self._addressing_classifier(
                    transcript=transcript,
                    speaker_count=None,
                    social_mode=inputs.social_mode,
                )
                _spf_evt = self._make_event(
                    event_id=self._new_event_id(),
                    event_type="signal_producer_fallback",
                    caused_by=[signal_evt_id],
                    payload_kind="signal",
                    extra_hash="addressing:wake_word_safety_net",
                )
                self._logger.log(dataclasses.replace(
                    _spf_evt,
                    payload_inline={
                        "primary_producer": (
                            type(self._minicpm_addressing_classifier).__name__
                            if self._minicpm_addressing_classifier is not None
                            else "NullMiniCPMAddressingClassifier"
                        ),
                        "fallback_producer": type(self._addressing_classifier).__name__,
                        "reason": "minicpm_addressing_unavailable",
                    },
                ))
                _addr_evt = self._make_event(
                    event_id=self._new_event_id(),
                    event_type="addressing_classified",
                    caused_by=_addressing_caused_by,
                    payload_kind="signal",
                )
                self._logger.log(dataclasses.replace(
                    _addr_evt,
                    retention_policy_id="signal_default_30d",
                    payload_inline={
                        "classifier_name": type(self._addressing_classifier).__name__,
                        "addressed": derive_user_addressed_agent(addressing_signal, inputs.social_mode, transcript),
                        "confidence": _confidence_float.get(addressing_signal.confidence, 0.5),
                        "evidence": addressing_signal.evidence,
                    },
                ))
            if addressing_signal is not None:
                inputs.user_addressed_agent = derive_user_addressed_agent(
                    addressing_signal, inputs.social_mode, transcript
                )

            # --- Deictic detector: sets deictic_reference + deictic_ambiguous on inputs ---
            if self._deictic_detector is not None:
                deictic_result = self._deictic_detector.classify(
                    transcript, [signal_evt_id]
                )
                inputs.deictic_reference = deictic_result.is_deictic
                inputs.deictic_ambiguous = deictic_result.is_ambiguous

            # Emit policy_decision event FIRST so event_id is available before enqueueing (nit 10).
            policy_evt_id = self._new_event_id()
            decision_future: asyncio.Future[SpeakDecision] = asyncio.get_running_loop().create_future()
            self._pending_decision_future = decision_future
            self._pending_signal_evt_id = signal_evt_id

            # DETERMINISM BOUNDARY: decide() is a pure function — no I/O.
            try:
                decision = self._speak_policy_decide(inputs, signal_evt_id, signal.p_backchannel)
            except Exception as exc:
                decision = SpeakDecision(
                    action_type="silence",
                    primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
                    supporting_reason_codes=[],
                    redacted_explanation=None,
                    caused_by=[signal_evt_id],
                    budget_bucket=None,
                    allowed_prosody_tags=[],
                    max_duration_ms=None,
                    response_content_source="no_synthesis",
                )
                self._logger.log(self._make_event(
                    event_id=self._new_event_id(),
                    event_type="policy_decision_error",
                    caused_by=[signal_evt_id],
                    payload_kind="signal",
                    extra_hash=type(exc).__name__,
                ))

            # signal_event_ids carries the ASR transcript event when one was
            # emitted (PR #144 P0): the transcript is one of the upstream signals
            # that drove the decision and a "why did you say that?" query must be
            # able to find it via the trace.
            decision_signal_event_ids = [signal_evt_id]
            if transcript_evt_id is not None:
                decision_signal_event_ids.append(transcript_evt_id)
            trace = _build_decision_trace(
                decision=decision,
                inputs=inputs,
                signal_event_ids=decision_signal_event_ids,
                decision_id=policy_evt_id,
                p_backchannel=signal.p_backchannel,
                retrieval_event_ids=[mre_event_id],
                **self._policy_threshold_kwargs,
            )
            trace_uri = self._decision_trace_store.write(trace)

            policy_evt = dataclasses.replace(
                self._make_event(
                    event_id=policy_evt_id,
                    event_type="policy_decision",
                    caused_by=[signal_evt_id, mre_event_id],
                    payload_kind="signal",
                    extra_hash=decision.action_type,
                ),
                retention_policy_id="decision_trace_30d",
                payload_ref=trace_uri,
                # Finding 5: inline the two operator-facing fields so the
                # display/audit consumer surfaces them without dereferencing
                # decision_trace://. Both are derived deterministically from
                # SpeakDecision; full trace remains at payload_ref.
                payload_inline={
                    "action_type": decision.action_type,
                    "primary_reason_code": decision.primary_reason_code.value,
                },
            )
            self._logger.log(policy_evt)

            _redacted_transcript = (
                dataclasses.replace(trace.user_transcript, value=None, value_ref=None)
                if trace.user_transcript is not None
                else None
            )
            trace_dict = dataclasses.asdict(dataclasses.replace(
                trace,
                redacted_explanation=None,
                sensitive_explanation_ref=None,
                user_transcript=_redacted_transcript,
            ))
            # Convert Enum values to strings for consistent hashing.
            trace_dict["primary_reason_code"] = trace.primary_reason_code.value
            trace_dict["supporting_reason_codes"] = [rc.value for rc in trace.supporting_reason_codes]
            payload_hash = hashlib.sha256(
                json.dumps(trace_dict, sort_keys=True).encode()
            ).hexdigest()
            trace_evt = dataclasses.replace(
                self._make_event(
                    event_id=self._new_event_id(),
                    event_type="decision_trace_emitted",
                    caused_by=[policy_evt_id],
                    payload_kind="model_output",
                    extra_hash=payload_hash,
                ),
                retention_policy_id="decision_trace_30d",
                payload_hash=payload_hash,
            )
            self._logger.log(trace_evt)
            decision_future.set_result(decision)

            # --- Explicit-remember detection (fires unconditionally on intent) ---
            # v0.1f: `transcript` is populated above by the ASR adapter on EOU
            # (or remains "" when asr_model is None, matching no phrase).
            matched, extracted = _detect_explicit_remember(transcript)
            if matched and extracted:
                cand_event_id = self._new_event_id()
                cand_payload = {
                    "item_id": None,
                    "store": "episodic",
                    "content": {"text": extracted},
                    "source_event_id": signal_evt_id,
                    "privacy_mode": inputs.privacy_mode,
                    "subject_class": "self",
                    "privacy_level": "user_content",
                    "mutability": "user_only",
                    "retention_policy_id": "ep_default_30d",
                    "sensitivity": "sensitive",
                }
                self._store_payload(cand_event_id, cand_payload)
                # caused_by includes the transcript event when emitted (PR #144 P0):
                # the candidate's content was extracted from the transcript text.
                cand_caused_by = [signal_evt_id]
                if transcript_evt_id is not None:
                    cand_caused_by.append(transcript_evt_id)
                cand_event = dataclasses.replace(
                    self._make_event(
                        event_id=cand_event_id,
                        event_type="memory_write_candidate",
                        caused_by=cand_caused_by,
                        payload_kind="memory_op",
                    ),
                    subject_class="self",
                    sensitivity="sensitive",
                    retention_policy_id="ep_default_30d",
                    payload_ref=f"orchestrator://{cand_event_id}",
                )
                self._logger.log(cand_event)

            # --- Explicit-forget detection (fires unconditionally on intent) ---
            # Payload carries a query string; SleepTimeAgent fans out across
            # wired stores via retrieve(query) → forget(item_id) per match.
            forget_matched, forget_query = _detect_explicit_forget(transcript)
            if forget_matched and forget_query:
                forget_event_id = self._new_event_id()
                forget_payload = {
                    "query": forget_query,
                    "source_event_id": signal_evt_id,
                    "privacy_mode": inputs.privacy_mode,
                }
                self._store_payload(forget_event_id, forget_payload)
                forget_caused_by = [signal_evt_id]
                if transcript_evt_id is not None:
                    forget_caused_by.append(transcript_evt_id)
                forget_event = dataclasses.replace(
                    self._make_event(
                        event_id=forget_event_id,
                        event_type="explicit_forget",
                        caused_by=forget_caused_by,
                        payload_kind="memory_op",
                    ),
                    subject_class="self",
                    sensitivity="sensitive",
                    retention_policy_id="ep_default_30d",
                    payload_ref=f"orchestrator://{forget_event_id}",
                )
                self._logger.log(forget_event)

            # --- User reduction command detection (v0.1g Task 9 / Wave 5) ---
            # Fires unconditionally on transcript; caused_by closes through
            # transcript_evt_id when available (invariant #1).
            cmd_type = detect_user_reduction_command(transcript)
            if cmd_type is not None:
                new_budget = apply_user_reduction_command(
                    cmd_type, inputs.proactivity_budget_remaining
                )
                inputs.proactivity_budget_remaining = new_budget
                if cmd_type == "quiet_mode":
                    inputs.quiet_mode_active = True
                cmd_payload = make_user_reduction_payload(cmd_type)
                cmd_evt_id = self._new_event_id()
                cmd_caused_by = [signal_evt_id]
                if transcript_evt_id is not None:
                    cmd_caused_by.append(transcript_evt_id)
                cmd_evt = dataclasses.replace(
                    self._make_event(
                        event_id=cmd_evt_id,
                        event_type="user_reduction_command_applied",
                        caused_by=cmd_caused_by,
                        payload_kind="signal",
                        extra_hash=cmd_type,
                    ),
                    subject_class="self",
                    sensitivity="safe",
                    retention_policy_id="commit_audit_30d",
                    payload_inline={
                        "command_type": cmd_payload.command_type,
                        "applied_at_ms": cmd_payload.applied_at_ms,
                    },
                )
                self._logger.log(cmd_evt)

            # Open a new batch window for T3.
            self._batch_close_event.clear()
            self._batch_open_event.set()

            await self._policy_decisions.put((signal_evt_id, decision_future, policy_evt_id))
            # _decision_in_flight is NOT reset here — T4 resets it after get() so that
            # any TurnSignals that arrive in _t2_inbox before T4 dequeues are still
            # coalesced (they see _decision_in_flight=True and emit turn_signal_coalesced).

    async def _foreground_stream_task(self) -> None:
        """T3: Per-batch bounded frame_iter → ForegroundModel → proposal_buffer."""
        while True:
            # Wait for T2 to open a new batch.
            await self._batch_open_event.wait()
            self._batch_open_event.clear()

            # Discard frames buffered between turn N's close and turn N+1's open.
            # These are silence/breath/early-N+1, not turn N+1's main content.
            # Without this, MiniCPM conditions on stale prefix and answers the wrong question.
            while not self._tee_to_foreground.empty():
                try:
                    self._tee_to_foreground.get_nowait()
                except asyncio.QueueEmpty:
                    break

            frame_iter = self._make_batch_frame_iter()
            async for proposal in self._foreground_model.process_stream(
                frame_iter,
                caused_by=[self._pending_signal_evt_id] if self._pending_signal_evt_id else [],
                context_items=tuple(self._pending_retrieved_items),
            ):
                self.proposal_buffer.append(proposal)
                self._first_proposal_event.set()

    def _make_batch_frame_iter(self) -> AsyncIterator[tuple[bytes, bytes | None]]:
        """Return a bounded async generator that terminates when _batch_close_event fires."""
        return self._bounded_frame_gen()

    def _consume_video_or_none(self) -> bytes | None:
        """Return the most-recent buffered video frame bytes (if any) and clear the buffer.

        When `vision_sidecar` is None (audio-only path), always returns None,
        preserving bit-for-bit backward compat with the existing live loop.
        """
        if self._vision_sidecar is None:
            return None
        pair = self._vision_sidecar.consume_pending_frame()
        return pair[0] if pair is not None else None

    async def _bounded_frame_gen(self):  # type: ignore[return]
        while not self._batch_close_event.is_set():
            # Race between next frame and batch-close.
            get_task = asyncio.ensure_future(self._tee_to_foreground.get())
            close_task = asyncio.ensure_future(self._batch_close_event.wait())
            try:
                done, pending = await asyncio.wait(
                    {get_task, close_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not get_task.done():
                    get_task.cancel()
                if not close_task.done():
                    close_task.cancel()
            for p in pending:
                p.cancel()

            if close_task in done:
                # Batch is closed; try to drain any already-received frame.
                if get_task in done:
                    frame_bytes, _ = get_task.result()
                    yield frame_bytes, self._consume_video_or_none()
                return

            # get_task completed.
            frame_bytes, _ = get_task.result()
            yield frame_bytes, self._consume_video_or_none()

    async def _synthesis_dispatch_task(self) -> None:
        """T4: Await policy_decision Future; grace window; snapshot; synthesize if approved."""
        while True:
            signal_evt_id, decision_future, policy_evt_id = await self._policy_decisions.get()
            # _decision_in_flight is NOT reset here — it stays True through the full
            # synthesis cycle (including play_task) so that TurnSignals arriving during
            # Kokoro playback are coalesced and emit coalesced_during_playback rather
            # than stacking a second synthesis on top of the first.  The reset moves to
            # the finally: block below, after play_task completes.
            decision = await decision_future

            # Close the batch for T3.
            self._batch_close_event.set()
            self._pending_retrieved_items = []

            if decision.action_type == "silence":
                self.proposal_buffer.clear()
                self._first_proposal_event.clear()
                self._decision_in_flight = False
                continue

            # Tool dispatch path — non-blocking; all events forwarded to logger.
            if decision.action_type == "tool_call" and self._tool_router is not None:
                tool_name = (decision.budget_bucket or "unknown_tool")
                request = ToolDispatchRequest(
                    tool_name=tool_name,
                    arguments={},
                    caused_by=[policy_evt_id],
                    routing_hint="fast",
                )
                dispatch_task = asyncio.get_running_loop().create_task(
                    self._tool_router.dispatch(request)
                )
                # Yield once so the router can register its cancel flag and
                # expose the tool_call_id before barge-in might fire (Task 7).
                await asyncio.sleep(0)
                if (
                    hasattr(self._tool_router, "_cancel_flags")
                    and self._tool_router._cancel_flags
                ):
                    self._inflight_tool_call_id = next(iter(self._tool_router._cancel_flags))
                try:
                    result = await dispatch_task
                finally:
                    self._inflight_tool_call_id = None
                for evt in result.events:
                    self._logger.log(evt)
                if self._tool_progress_emitter is not None:
                    now_ms = int(__import__("time").monotonic() * 1000)
                    if self._tool_progress_emitter.should_emit_filler(result.tool_call_id, now_ms):
                        self._tool_progress_emitter.record_filler(result.tool_call_id, now_ms)
                self.proposal_buffer.clear()
                self._first_proposal_event.clear()
                self._decision_in_flight = False
                continue

            # Grace window: wait for at least one proposal BEFORE snapshot (edge case h).
            if not self.proposal_buffer:
                try:
                    await asyncio.wait_for(
                        self._first_proposal_event.wait(),
                        timeout=self._proposal_batch_window_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    _close_ms = int(time.monotonic() * 1000)
                    _skip_evt = self._make_event(
                        event_id=self._new_event_id(),
                        event_type="synthesis_skipped_no_proposal",
                        caused_by=[policy_evt_id],
                        payload_kind="signal",
                    )
                    self._logger.log(dataclasses.replace(
                        _skip_evt,
                        payload_inline={
                            "dispatcher_state": "timeout_waiting_for_first_proposal",
                            "batch_window_ms": self._proposal_batch_window_ms,
                            "batch_open_at_ms": _close_ms - self._proposal_batch_window_ms,
                            "batch_close_at_ms": _close_ms,
                            "signal_evt_id": signal_evt_id,
                        },
                    ))
                    self.proposal_buffer.clear()
                    self._first_proposal_event.clear()
                    self._decision_in_flight = False
                    continue

            # SNAPSHOT — no await between these two lines (race-freedom).
            snapshot = list(self.proposal_buffer)
            self.proposal_buffer.clear()
            self._first_proposal_event.clear()

            text = _best_proposal_text(snapshot)
            gen_event_id = self._audio_output.start_generation(caused_by=[policy_evt_id])
            chunks = self._tts_adapter.synthesize(text, decision.allowed_prosody_tags)

            # Wrap play() in a task so _fire_barge_in can cancel it (Task 6).
            play_task: asyncio.Task[None] = asyncio.get_running_loop().create_task(
                self._audio_output.play(chunks, gen_event_id),
                name=f"play-{gen_event_id}",
            )
            self._audio_output.set_generation_task(play_task)
            try:
                await play_task
            except asyncio.CancelledError:
                pass  # hard-cancel path; cancel_generation() already logged the event
            except Exception as exc:
                self._logger.log(self._make_event(
                    event_id=self._new_event_id(),
                    event_type="tts_adapter_error",
                    caused_by=[gen_event_id],
                    payload_kind="signal",
                    extra_hash=type(exc).__name__,
                ))
                if self._audio_output.is_playing:
                    self._audio_output.request_stop(caused_by=[gen_event_id])
            finally:
                self._audio_output.set_generation_task(None)
                self._decision_in_flight = False

    async def _smart_path_task(self) -> None:
        """T5: Drain smart_path_queue; drive BackgroundReasoner; inject context (OQ-12)."""
        from companion_harness.background_reasoner import (
            BackgroundReasonerBudgetExhausted,
            ToolReasonerResult,
        )
        while True:
            request = await self._smart_path_queue.get()
            event_iter = self._background_reasoner.select_and_call(request)
            last_event_id = request.caused_by[0] if request.caused_by else ""
            budget_exc: BackgroundReasonerBudgetExhausted | None = None
            try:
                async for evt in event_iter:
                    self._logger.log(evt)
                    last_event_id = evt.event_id
            except BackgroundReasonerBudgetExhausted as exc:
                budget_exc = exc
                self._logger.log(self._make_budget_exhausted_event(
                    exc, caused_by=[last_event_id] if last_event_id else list(request.caused_by)
                ))
            result = ToolReasonerResult(
                tool_call_id=request.tool_name,
                tool_name=request.tool_name,
                summary_text=(
                    f"truncated:{request.tool_name}:{budget_exc.budget_kind}"
                    if budget_exc is not None
                    else f"completed:{request.tool_name}"
                ),
                caused_by=[last_event_id] if last_event_id else list(request.caused_by),
            )
            memory_items = self._background_reasoner.summarize(result)
            self._foreground_model.set_context(memory_items)

    def _make_budget_exhausted_event(
        self,
        exc: "BackgroundReasonerBudgetExhausted",
        caused_by: list[str],
    ) -> "Event":
        now_ms = int(time.monotonic() * 1000)
        event_id = f"{self._session_id}-orch-budgetex-{now_ms}"
        payload_hash = hashlib.sha256(
            f"reasoner_budget_exhausted:{event_id}:{exc.budget_kind}".encode()
        ).hexdigest()[:16]
        return Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version="0.1",
            seq_no=self._next_seq(),
            event_type="reasoner_budget_exhausted",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source="realtime_orchestrator",
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="tool_call_audit_30d",
            payload_inline={
                "budget_kind": exc.budget_kind,
                "limit": exc.limit,
                "observed": exc.observed,
            },
        )

    def _speak_policy_decide(
        self,
        inputs: PolicyInputs,
        signal_evt_id: str,
        p_backchannel: float,
    ) -> SpeakDecision:
        # Policy threshold kwargs are populated by _snapshot_config() at the EOU
        # boundary; when config_store is None they remain empty and the callable
        # uses its own defaults (backward compatible).
        return self._speak_policy_fn(
            inputs, [signal_evt_id], p_backchannel, **self._policy_threshold_kwargs
        )

    def _snapshot_config(self) -> None:
        """Read all 12 Tier-B keys at the EOU boundary; apply to orchestrator + detectors.

        No-op when ``config_store`` is None (backward compatible). All reads are
        synchronous (no await) so the snapshot is atomic relative to the policy
        decision. See docs/design-config-and-dashboard.md §3, §6.
        """
        if self._config_store is None:
            return
        cfg = self._config_store

        # Orchestrator-level keys → self state (apply within this iteration).
        self._proposal_batch_window_ms = cfg.get("orchestrator.proposal_batch_window_ms")
        self._hard_cancel_after_ms = cfg.get("orchestrator.hard_cancel_after_ms")
        self._p_speech_thresh = cfg.get("orchestrator.p_speech_thresh")
        self._p_backchannel_thresh = cfg.get("orchestrator.p_backchannel_thresh")

        # Policy keys → kwargs threaded into decide() in the same no-await window.
        self._policy_threshold_kwargs = {
            "backchannel_threshold": cfg.get("policy.backchannel_threshold"),
            "audio_visual_conflict_threshold": cfg.get("policy.audio_visual_conflict_threshold"),
            "grounding_confidence_threshold": cfg.get("policy.grounding_confidence_threshold"),
        }

        # Detector keys → setters; take effect on the NEXT frame after this EOU.
        self._vad_detector.update_thresholds(
            speech_threshold=cfg.get("detectors.vad.speech_threshold"),
            silence_onset_ms=cfg.get("detectors.vad.silence_onset_ms"),
        )
        self._smart_turn_detector.update_thresholds(
            silence_onset_ms=cfg.get("detectors.smart_turn.silence_onset_ms"),
            silence_rms_threshold=cfg.get("detectors.smart_turn.silence_rms_threshold"),
        )
        self._backchannel_classifier.update_threshold(
            emit_threshold=cfg.get("detectors.backchannel.emit_threshold"),
        )

        # Reasoner budget keys (v0.2a T3): always read; apply when a reasoner is wired.
        wcs = cfg.get("reasoner.budget_wall_clock_s")
        sc = cfg.get("reasoner.budget_step_count")
        if self._background_reasoner is not None:
            if wcs is not None and hasattr(self._background_reasoner, "_budget_wall_clock_s"):
                self._background_reasoner._budget_wall_clock_s = float(wcs)
            if sc is not None and hasattr(self._background_reasoner, "_budget_step_count"):
                self._background_reasoner._budget_step_count = int(sc)

    # ------------------------------------------------------------------
    # Barge-in helpers (Task 6)
    # ------------------------------------------------------------------

    def _should_emit_speech_onset(self, frame: _VadOnsetFrame) -> bool:
        return (
            frame.p_speech > self._p_speech_thresh
            and self._audio_output.is_playing
            and not self._speech_onset_debounce_active
        )

    def is_barge_in_trigger(self) -> bool:
        return (
            self._audio_output.is_playing
            and not self._audio_output.is_synthesizing  # don't barge-in during TTS synthesis window
            and not self._barge_in_in_flight
            and self._latest_p_backchannel < self._p_backchannel_thresh
        )

    async def _fire_barge_in(self, onset_evt_id: str) -> None:
        """Graceful stop + bounded hard-cancel fallback. Fire-and-forget background task.

        Accesses AudioOutputController.generation_task from a separate asyncio.Task
        in the same event loop. Safe by virtue of single-threaded asyncio; do NOT
        call generation_task from a different OS thread.
        """
        play_task = self._audio_output.generation_task
        if play_task is None or play_task.done():
            self._logger.log(self._make_event(
                event_id=self._new_event_id(),
                event_type="barge_in_trigger_no_op",
                caused_by=[onset_evt_id],
                payload_kind="signal",
            ))
            self._barge_in_in_flight = False
            return
        try:
            stop_requested_evt_id = self._audio_output.request_stop(caused_by=[onset_evt_id])
            # Cancel any in-flight tool dispatch (Task 7).
            if self._tool_router is not None and self._inflight_tool_call_id is not None:
                await self._tool_router.cancel(self._inflight_tool_call_id)
            try:
                await asyncio.wait_for(
                    asyncio.shield(play_task),
                    timeout=self._hard_cancel_after_ms / 1000.0,
                )
            except asyncio.TimeoutError:
                self._audio_output.cancel_generation(caused_by=[stop_requested_evt_id])
                try:
                    await play_task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            # If _fire_barge_in itself is cancelled (e.g. via stop()) while shield()-waiting,
            # play_task keeps running behind the shield. Cancel it explicitly.
            if not play_task.done():
                play_task.cancel()
                try:
                    await play_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._barge_in_in_flight = False

    # ------------------------------------------------------------------
    # Memory payload helpers
    # ------------------------------------------------------------------

    def _fifo_evict_if_full(self) -> None:
        """Evict oldest entry when the payload dict is at capacity."""
        while len(self._memory_event_payloads) >= MEMORY_EVENT_PAYLOADS_CAP:
            self._memory_event_payloads.popitem(last=False)

    def _store_payload(self, event_id: str, payload: dict) -> None:
        self._fifo_evict_if_full()
        self._memory_event_payloads[event_id] = payload

    # ------------------------------------------------------------------
    # Event construction helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _new_event_id(self) -> str:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        return f"{self._session_id}-sro-{seq}-{now_ms}"

    def _emit(self, event_type: str, caused_by: list[str], payload_kind: str) -> Event:
        evt = self._make_event(
            event_id=self._new_event_id(),
            event_type=event_type,
            caused_by=caused_by,
            payload_kind=payload_kind,
        )
        self._logger.log(evt)
        return evt

    def _make_event(
        self,
        event_id: str,
        event_type: str,
        caused_by: list[str],
        payload_kind: str,
        extra_hash: str = "",
    ) -> Event:
        now_ms = int(time.monotonic() * 1000)
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}:{extra_hash}".encode()
        ).hexdigest()[:16]
        return Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=self._seq,
            event_type=event_type,
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=_SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind=payload_kind,  # type: ignore[arg-type]
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="default",
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _best_proposal_text(proposals: list[ThinkerProposal]) -> str:
    if not proposals:
        return ""
    return max(proposals, key=lambda p: p.confidence).content
