"""StreamingRealtimeOrchestrator — async task-graph live-loop pump (live-loop Task 5b).

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
"""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from typing import Any

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import IngestSession
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.speak_policy import decide as _default_speak_policy_decide
from companion_harness.tts_adapter import TtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector

__all__ = ["StreamingRealtimeOrchestrator"]

_SCHEMA_VERSION = "0.1"
_SOURCE = "streaming_realtime_orchestrator"

_AUDIO_QUEUE_BOUND = 64
_TURN_SIGNAL_QUEUE_BOUND = 32
_POLICY_DECISIONS_QUEUE_BOUND = 8


def _drop_oldest_put(q: asyncio.Queue[Any], item: Any, logger: EventLogger, caused_by: list[str]) -> None:
    """Put item onto a bounded audio queue; drop oldest on overflow + log."""
    try:
        q.put_nowait(item)
    except asyncio.QueueFull:
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        q.put_nowait(item)
        logger.log(_make_degrade_event(caused_by))


def _make_degrade_event(caused_by: list[str]) -> Event:
    now_ms = int(time.monotonic() * 1000)
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

        # Internal queues
        self._tee_to_detectors: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=_AUDIO_QUEUE_BOUND)
        self._tee_to_foreground: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=_AUDIO_QUEUE_BOUND)
        self._turn_signals: asyncio.Queue[tuple[TurnSignal, str]] = asyncio.Queue(maxsize=_TURN_SIGNAL_QUEUE_BOUND)
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
        self._pending_decision_future: asyncio.Future[SpeakDecision] | None = None
        self._pending_signal_evt_id: str = ""

        # Signal history for determinism boundary (sorted before crossing into policy_inputs_builder)
        self._signal_history: list[TurnSignal] = []

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

    async def stop(self) -> None:
        """Cancel all tasks, emit orchestrator_stopped, drain logger."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()

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

    # ------------------------------------------------------------------
    # Internal tasks
    # ------------------------------------------------------------------

    async def _audio_tee_task(self) -> None:
        """Read audio_in; fan to tee_to_detectors and tee_to_foreground (drop-oldest)."""
        while True:
            frame_bytes, chunk_event_id = await self._audio_in.get()
            item = (frame_bytes, chunk_event_id)
            _drop_oldest_put(self._tee_to_detectors, item, self._logger, ["_dropped_before_enqueue"])
            _drop_oldest_put(self._tee_to_foreground, item, self._logger, ["_dropped_before_enqueue"])

    async def _detector_fanout_task(self) -> None:
        """T1: Fan audio frames to all detectors; forward TurnSignals to policy gate."""
        while True:
            frame_bytes, chunk_event_id = await self._tee_to_detectors.get()
            caused_by = [chunk_event_id]

            for detector in (self._vad_detector, self._smart_turn_detector, self._backchannel_classifier):
                sig: TurnSignal | None = detector.process_frame(frame_bytes, caused_by)
                if sig is not None:
                    signal_evt_id = sig.evidence_event_ids[0] if sig.evidence_event_ids else chunk_event_id
                    await self._turn_signals.put((sig, signal_evt_id))

    async def _policy_gate_task(self) -> None:
        """T2: Gate each EOU TurnSignal through SpeakPolicy.decide(), emit policy_decision."""
        while True:
            signal, signal_evt_id = await self._turn_signals.get()

            # Coalescing guard (edge case i): skip if a decision_future is still pending.
            if self._pending_decision_future is not None and not self._pending_decision_future.done():
                self._emit("turn_signal_coalesced", [self._pending_signal_evt_id], "signal")
                continue

            # Maintain signal history (for determinism boundary).
            self._signal_history.append(signal)
            # Sort by evidence_event_ids[0] lexicographic — determinism boundary (§6).
            sorted_history = sorted(
                self._signal_history,
                key=lambda s: s.evidence_event_ids[0] if s.evidence_event_ids else "",
            )

            inputs = self._policy_inputs_builder(signal, sorted_history)

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
                )
                self._logger.log(self._make_event(
                    event_id=self._new_event_id(),
                    event_type="policy_decision_error",
                    caused_by=[signal_evt_id],
                    payload_kind="signal",
                    extra_hash=type(exc).__name__,
                ))

            self._logger.log(self._make_event(  # policy_evt_id already allocated above
                event_id=policy_evt_id,
                event_type="policy_decision",
                caused_by=[signal_evt_id],
                payload_kind="signal",
                extra_hash=decision.action_type,
            ))
            decision_future.set_result(decision)

            # Open a new batch window for T3.
            self._batch_close_event.clear()
            self._batch_open_event.set()

            await self._policy_decisions.put((signal_evt_id, decision_future, policy_evt_id))

    async def _foreground_stream_task(self) -> None:
        """T3: Per-batch bounded frame_iter → ForegroundModel → proposal_buffer."""
        while True:
            # Wait for T2 to open a new batch.
            await self._batch_open_event.wait()
            self._batch_open_event.clear()

            frame_iter = self._make_batch_frame_iter()
            async for proposal in self._foreground_model.process_stream(
                frame_iter,
                caused_by=[self._pending_signal_evt_id] if self._pending_signal_evt_id else [],
            ):
                self.proposal_buffer.append(proposal)
                self._first_proposal_event.set()

    def _make_batch_frame_iter(self) -> AsyncIterator[tuple[bytes, bytes | None]]:
        """Return a bounded async generator that terminates when _batch_close_event fires."""
        return self._bounded_frame_gen()

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
                    yield frame_bytes, None
                return

            # get_task completed.
            frame_bytes, _ = get_task.result()
            yield frame_bytes, None

    async def _synthesis_dispatch_task(self) -> None:
        """T4: Await policy_decision Future; grace window; snapshot; synthesize if approved."""
        while True:
            signal_evt_id, decision_future, policy_evt_id = await self._policy_decisions.get()
            decision = await decision_future

            # Close the batch for T3.
            self._batch_close_event.set()

            if decision.action_type == "silence":
                self.proposal_buffer.clear()
                self._first_proposal_event.clear()
                continue

            # Grace window: wait for at least one proposal BEFORE snapshot (edge case h).
            if not self.proposal_buffer:
                try:
                    await asyncio.wait_for(
                        self._first_proposal_event.wait(),
                        timeout=self._proposal_batch_window_ms / 1000.0,
                    )
                except asyncio.TimeoutError:
                    self._emit("synthesis_skipped_no_proposal", [policy_evt_id], "signal")
                    self.proposal_buffer.clear()
                    self._first_proposal_event.clear()
                    continue

            # SNAPSHOT — no await between these two lines (race-freedom).
            snapshot = list(self.proposal_buffer)
            self.proposal_buffer.clear()
            self._first_proposal_event.clear()

            text = _best_proposal_text(snapshot)
            gen_event_id = self._audio_output.start_generation(caused_by=[policy_evt_id])

            try:
                chunks = self._tts_adapter.synthesize(text, decision.allowed_prosody_tags)
                await self._audio_output.play(chunks, gen_event_id)
            except Exception as exc:
                self._logger.log(self._make_event(
                    event_id=self._new_event_id(),
                    event_type="tts_adapter_error",
                    caused_by=[gen_event_id],
                    payload_kind="signal",
                    extra_hash=type(exc).__name__,
                ))
                self._audio_output.request_stop(caused_by=[gen_event_id])

    def _speak_policy_decide(
        self,
        inputs: PolicyInputs,
        signal_evt_id: str,
        p_backchannel: float,
    ) -> SpeakDecision:
        return self._speak_policy_fn(inputs, [signal_evt_id], p_backchannel)

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
