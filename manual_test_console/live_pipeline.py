"""Live pipeline factory for the manual-test console.

Wires StreamingRealtimeOrchestrator into the manual-test server so a real
microphone stream produces VAD frames, turn signals, policy decisions, and
ThinkerProposal events on the display WS.

Out of scope (deferred to a follow-up PR):
  - Voice-back (TTS / audio sink emits no audio bytes).

Detector models:
  - Default (real models): SileroVADModel + PipecatSmartTurnModel +
    ASRLexiconBackchannelModel. Pre-constructed by the server at startup and
    passed in via the `*_model_factory` knobs below.
  - Stub mode (`use_stubs=True`): EnergyVADModel / SilenceSmartTurnModel /
    ZeroBackchannelModel — CPU-only, deterministic, used by the contract tests
    and as a manual-test fallback if a real-model load fails.

Adapter discipline:
  - This module may import MiniCPMStreamingModel (which imports torch on b200).
  - speak_policy.py and test files MUST NOT import a model SDK.
  - Tests inject a fake foreground model via build_live_pipeline(foreground_model=...).
"""

from __future__ import annotations

import array
import asyncio
import math
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import IngestSession
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import PolicyInputs, ThinkerProposal, TurnSignal
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector

__all__ = [
    "LivePipeline",
    "build_live_pipeline",
    "EnergyVADModel",
    "SilenceSmartTurnModel",
    "ZeroBackchannelModel",
    "NoopTtsAdapter",
    "SharedLoggerProxy",
]


class SharedLoggerProxy:
    """EventLogger proxy that shields a shared logger from per-session stop().

    StreamingRealtimeOrchestrator.stop() calls `await self._logger.stop()` on
    the logger it was constructed with. In the manual-test server the
    EventLogger is owned by the aiohttp Application and is shared across
    every /ws/ingest session — letting one session shut it down would break
    the server. This proxy forwards .log() to the shared logger but no-ops
    .start() / .stop(), keeping ownership with the Application lifecycle.
    """

    def __init__(self, inner: EventLogger) -> None:
        self._inner = inner

    def log(self, event: Any) -> None:
        self._inner.log(event)

    def subscribe(self, callback: Any) -> None:
        self._inner.subscribe(callback)

    def unsubscribe(self, callback: Any) -> None:
        self._inner.unsubscribe(callback)

    async def start(self) -> None:
        return None  # shared logger lifecycle is owned by the Application

    async def stop(self) -> None:
        return None  # shared logger lifecycle is owned by the Application

    @property
    def _task(self) -> Any:
        return self._inner._task  # for /healthz logger_drain_running check


# ---------------------------------------------------------------------------
# CPU-friendly model substitutes
# ---------------------------------------------------------------------------


class EnergyVADModel:
    """Energy-based VAD substitute. Returns p_speech in [0, 1] from frame RMS.

    Trade-off vs Silero VAD: deterministic, zero-dep, ~microsecond per frame.
    Loses Silero's noise robustness — false positives on loud background noise.
    Adequate for manual-test verification ("speaking → high, silent → low").
    Real Silero VAD wiring is a follow-up issue.
    """

    def __init__(self, rms_floor: float = 200.0, rms_ceiling: float = 3000.0) -> None:
        self._floor = rms_floor
        self._ceil = rms_ceiling

    def __call__(self, frame: bytes) -> float:
        if len(frame) < 2:
            return 0.0
        samples = array.array("h", frame[: len(frame) - len(frame) % 2])
        if not samples:
            return 0.0
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        if rms <= self._floor:
            return 0.0
        if rms >= self._ceil:
            return 1.0
        return (rms - self._floor) / (self._ceil - self._floor)


class SilenceSmartTurnModel:
    """SmartTurnModel stub — returns (p_done=0.6, p_continue=0.4) at silence candidates.

    Real Pipecat Smart Turn v3 wiring is a follow-up issue. The orchestrator
    invokes this only at silence-candidate moments (see SmartTurnDetector docstring).
    """

    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.6, 0.4


class ZeroBackchannelModel:
    """BackchannelModel stub — always returns p_backchannel=0.0.

    A real frame-level backchannel classifier is a follow-up issue.
    """

    def __call__(self, frame: bytes) -> float:
        return 0.0


# ---------------------------------------------------------------------------
# No-op TTS adapter + audio sink (voice-back deferred to a follow-up PR)
# ---------------------------------------------------------------------------


class NoopTtsAdapter:
    """TtsAdapter that yields a single empty chunk.

    Keeps the orchestrator's gating invariant intact (SpeakPolicy.decide()
    called once per batch before any synthesis byte) without producing any
    audio bytes. Voice-back will replace this in a follow-up PR.
    """

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b""


async def _noop_audio_sink(chunk: bytes) -> None:
    return None


# ---------------------------------------------------------------------------
# policy_inputs_builder
# ---------------------------------------------------------------------------


def _live_policy_inputs_builder(
    signal: TurnSignal, signal_history: list[TurnSignal]
) -> PolicyInputs:
    """Build PolicyInputs from a TurnSignal + sorted history (invariant #5).

    No wall-clock reads, no jitter. Mirrors realtime_loop._signals_to_policy_inputs
    but uses the current `signal` directly. user_addressed_agent defaults to
    False — until a Thinker proposal or explicit address marker is wired, the
    policy must default to silence on EOU rather than freely speak.
    """
    max_p_done = max((s.p_done for s in signal_history), default=signal.p_done)
    max_p_continue = max((s.p_continue for s in signal_history), default=signal.p_continue)
    user_speaking = max_p_done <= max_p_continue

    return PolicyInputs(
        user_speaking=user_speaking,
        eou_probability=max_p_done,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
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


# ---------------------------------------------------------------------------
# LivePipeline
# ---------------------------------------------------------------------------


@dataclass
class LivePipeline:
    """Per-session live-loop state. Owns the orchestrator + audio_in queue."""

    session_id: str
    audio_in: asyncio.Queue[tuple[bytes, str]]
    orchestrator: StreamingRealtimeOrchestrator

    async def start(self) -> None:
        await self.orchestrator.start()

    async def stop(self) -> None:
        await self.orchestrator.stop()

    def push_audio(self, frame_bytes: bytes, raw_audio_event_id: str) -> None:
        """Enqueue an audio frame onto the orchestrator's audio_in queue.

        Drop-oldest on overflow so the realtime ingest path is never blocked
        (invariant #10). The orchestrator's internal queues then fan out via
        _audio_tee_task.
        """
        try:
            self.audio_in.put_nowait((frame_bytes, raw_audio_event_id))
        except asyncio.QueueFull:
            try:
                self.audio_in.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self.audio_in.put_nowait((frame_bytes, raw_audio_event_id))
            except asyncio.QueueFull:
                pass


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_live_pipeline(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session: IngestSession,
    foreground_duplex_model: Any,
    decision_trace_dir: Path | None = None,
    proposal_batch_window_ms: int = 200,
    vad_model: Any = None,
    smart_turn_model: Any = None,
    backchannel_model: Any = None,
    use_stubs: bool = False,
) -> LivePipeline:
    """Construct a LivePipeline for one ingest session.

    `foreground_duplex_model` must satisfy `StreamingDuplexModel` (it is wrapped
    in a `ForegroundModel`). At server startup the real instance is a singleton
    `MiniCPMStreamingModel`; tests inject a fake.

    `vad_model`, `smart_turn_model`, `backchannel_model` are detector model
    instances satisfying the corresponding Protocols. They are typically
    pre-loaded singletons (e.g. SileroVADModel, PipecatSmartTurnModel,
    ASRLexiconBackchannelModel) constructed once at server startup. When
    `use_stubs=True` (or any individual model is None), the CPU stubs
    (EnergyVADModel / SilenceSmartTurnModel / ZeroBackchannelModel) are used
    in place of any missing detector. Tests rely on this stubs-by-default
    behavior to avoid loading torch / ONNX.

    The shared `logger` is wrapped in a `SharedLoggerProxy` so this session's
    StreamingRealtimeOrchestrator.stop() cannot shut down the Application-owned
    EventLogger on disconnect.
    """
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    shielded_logger = SharedLoggerProxy(logger)

    if use_stubs:
        vad_model = EnergyVADModel()
        smart_turn_model = SilenceSmartTurnModel()
        backchannel_model = ZeroBackchannelModel()
    else:
        if vad_model is None:
            vad_model = EnergyVADModel()
        if smart_turn_model is None:
            smart_turn_model = SilenceSmartTurnModel()
        if backchannel_model is None:
            backchannel_model = ZeroBackchannelModel()

    vad = VADDetector(
        model=vad_model,
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
        speech_threshold=0.5,
        silence_onset_ms=300,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=smart_turn_model,
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
    )
    backchannel = BackchannelClassifier(
        model=backchannel_model,
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
    )
    foreground = ForegroundModel(
        model=foreground_duplex_model,
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
    )
    audio_output = AudioOutputController(
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
        sink=_noop_audio_sink,
    )

    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=backchannel,
        policy_inputs_builder=_live_policy_inputs_builder,
        foreground_model=foreground,
        audio_output=audio_output,
        tts_adapter=NoopTtsAdapter(),
        proposal_batch_window_ms=proposal_batch_window_ms,
        decision_trace_dir=decision_trace_dir,
    )

    return LivePipeline(session_id=session_id, audio_in=audio_in, orchestrator=orch)
