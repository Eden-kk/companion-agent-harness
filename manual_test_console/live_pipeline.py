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
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from companion_harness.addressing_classifier import (
    WakeWordAddressingClassifier,
    _NullMiniCPMAddressingClassifier,
)
from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.av_conflict_scorer import AudioVisualConflictScorer, _NullAudioVisualConflictScorer
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import IngestSession
from companion_harness.native_duplex_eou import _NullNativeDuplexEouSource
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import MemoryItem, PolicyInputs, ThinkerProposal, TurnSignal
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector
from companion_harness.urgency_scorer import UrgencyScorer, _NullUrgencyScorer
from manual_test_console.config_schema import ALLOWLIST
from manual_test_console.config_store import ConfigStore

__all__ = [
    "LivePipeline",
    "build_live_pipeline",
    "build_config_store",
    "EnergyVADModel",
    "SilenceSmartTurnModel",
    "ZeroBackchannelModel",
    "EmptyTranscriptASRModel",
    "NoopTtsAdapter",
    "SharedLoggerProxy",
    "WebSocketAudioSink",
    "AudioOutSinkTarget",
]


def build_config_store(config_path: Path | None = None) -> ConfigStore:
    """Construct the server-global Tier-B ConfigStore singleton.

    Loads defaults from :data:`ALLOWLIST`, then optionally overlays
    ``config_path`` if provided and the file exists. The store is shared across
    every session per design doc §3 (server-global, per-process). Task E will
    expose the write side via /config/patch and /config/reset on the same
    instance.
    """
    store = ConfigStore(ALLOWLIST)
    if config_path is not None and config_path.exists():
        store.load_from_yaml(config_path)
    return store


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


class EmptyTranscriptASRModel:
    """ASRModel stub — always returns "". Used under --use-stubs to avoid loading
    faster-whisper on machines without GPU. The orchestrator's behavior with this
    stub is identical to asr_model=None (transcript stays "").
    """

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        return ""


class EmptyMemoryStore:
    """No-op MemoryManager stub: retrieve always returns [], writes are no-ops."""

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        return None

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        return []

    def forget(self, item_id: str) -> None:
        return None

    def hard_delete(self, item_id: str) -> None:
        return None


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


class AudioOutSinkTarget(Protocol):
    """Sink target for WebSocketAudioSink — typically the server's AudioOutBroker.

    Decoupled from server.py to keep live_pipeline.py importable in tests.
    """

    def publish(self, session_id: str, seq: int, chunk: bytes) -> None: ...


class WebSocketAudioSink:
    """AudioSink that forwards synthesized audio chunks to a fan-out broker.

    Each call publishes one (session_id, seq, chunk) tuple to the broker, which
    then pushes it onto every connected /ws/audio_out listener's bounded queue.
    The sink itself never blocks: the broker uses put_nowait + drop-oldest per
    listener queue (invariant #10). The sink is a SIDE EFFECT downstream of the
    existing `assistant_audio_buffer_*` events emitted by AudioOutputController;
    it does not emit new events itself.
    """

    def __init__(self, session_id: str, broker: AudioOutSinkTarget) -> None:
        self._session_id = session_id
        self._broker = broker
        self._seq = 0

    async def __call__(self, chunk: bytes) -> None:
        self._seq += 1
        self._broker.publish(self._session_id, self._seq, chunk)


# ---------------------------------------------------------------------------
# policy_inputs_builder
# ---------------------------------------------------------------------------


def _make_live_policy_inputs_builder(
    is_playing_fn: Callable[[], bool] | None = None,
    vision_sidecar: Any = None,
    av_scorer: AudioVisualConflictScorer | None = None,
    urgency_scorer: UrgencyScorer | None = None,
) -> Callable[[TurnSignal, list[TurnSignal]], PolicyInputs]:
    """Factory: return a builder closure with `assistant_speaking` bound to
    `is_playing_fn` (typically `AudioOutputController.is_playing`).

    Closure approach (chosen over an orchestrator parameter change) keeps the
    `Callable[[TurnSignal, list[TurnSignal]], PolicyInputs]` signature stable,
    so existing test fakes for `policy_inputs_builder` continue to type-check.
    When `is_playing_fn is None`, `assistant_speaking` falls back to `False`
    (backward compat for tests that call `_live_policy_inputs_builder` directly).

    `vision_sidecar` (optional): when provided, the builder reads
    `scene_change_score` and `grounding_confidence` from the sidecar's
    accessors; otherwise both default to `0.0`.

    `av_scorer` (optional): the `AudioVisualConflictScorer` used to source
    `audio_visual_conflict_score`. Defaults to `_NullAudioVisualConflictScorer`
    (returns 0.0; UNAVAILABLE: #168).

    `urgency_scorer` (optional): the `UrgencyScorer` used to source
    `urgency_score`. Defaults to `_NullUrgencyScorer` (returns 0.0;
    UNAVAILABLE: #171).
    """
    _av: AudioVisualConflictScorer = av_scorer if av_scorer is not None else _NullAudioVisualConflictScorer()
    _urgency: UrgencyScorer = urgency_scorer if urgency_scorer is not None else _NullUrgencyScorer()

    def _builder(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
        """Build PolicyInputs from a TurnSignal + sorted history.

        No wall-clock reads, no jitter. Mirrors `realtime_loop._signals_to_policy_inputs`
        but uses the current `signal` directly. Mode-field defaults follow the spec
        enumerations (architecture-v0.1.md:793-803):
          - `privacy_mode = "normal"` (spec default; not no_memory / no_camera_memory / etc.)
          - `current_task_mode = "normal"` (spec default; not creative_focus / cooking / etc.)
          - `social_mode = "user_addressing_agent"` (single-user manual-test default; multi-party
            scenarios would set "user_addressing_other" / "group_conversation" / "background_presence"
            and trip `_BLOCKING_SOCIAL_MODES` in speak_policy)
          - `risk_mode = "normal"` (spec default)

        `user_addressed_agent` is set to `False` here as a placeholder. The
        orchestrator's `AddressingClassifier` (wired below in `build_live_pipeline`)
        overrides this value after ASR using the 3-tier rule:
            - explicit wake-word match  -> True
            - multi-speaker detected    -> False (diarization not wired in v0.1f)
            - implicit fallback         -> social_mode == "user_addressing_agent"
        The builder runs BEFORE ASR/classifier, so `False` is safe: the classifier
        is the authoritative source of `user_addressed_agent` on the live path.
        See companion_harness/addressing_classifier.py and issue #139.

        `assistant_speaking` reflects `AudioOutputController.is_playing` when
        `is_playing_fn` was bound by the factory; otherwise `False`. The policy
        gate at `speak_policy.decide()` uses this to avoid talking over itself.

        `grounding_confidence = 0.0` (NOT `1.0`) because no grounding model is
        wired in v0.1f. Defaulting to `1.0` would SUPPRESS the policy gate
        `inputs.deictic_reference and inputs.grounding_confidence < 0.5`
        (VISUAL_LOW_CONFIDENCE silence) even when "no vision" is the truth.
        With `0.0`, the gate fires correctly once a future visual scene scorer
        sets `deictic_reference=True`. Today `deictic_reference` is hardcoded
        `False` so the gate is not reached — the change is forward-safety.
        Follow-up (cleaner): add `grounding_available: bool` to PolicyInputs
        so the policy can distinguish "no vision" from "vision low-confidence".
        """
        max_p_done = max((s.p_done for s in signal_history), default=signal.p_done)
        max_p_continue = max((s.p_continue for s in signal_history), default=signal.p_continue)
        user_speaking = max_p_done <= max_p_continue

        assistant_speaking = bool(is_playing_fn()) if is_playing_fn is not None else False

        social_mode = "user_addressing_agent"

        scene_score = (
            vision_sidecar.last_scene_change_score()
            if vision_sidecar is not None
            else 0.0  # UNAVAILABLE: #166 — real CLIP cosine scorer pending model wiring
        )

        return PolicyInputs(
            user_speaking=user_speaking,
            eou_probability=max_p_done,
            assistant_speaking=assistant_speaking,
            scene_change_score=scene_score,
            deictic_reference=False,
            user_addressed_agent=False,  # placeholder; AddressingClassifier overrides post-ASR.
            urgency_score=_urgency.score("", None),  # UNAVAILABLE: #171
            proactivity_budget_remaining={},
            privacy_mode="normal",
            current_task_mode="normal",
            social_mode=social_mode,
            risk_mode="normal",
            cooldown_state={},
            attachment_risk_level=0.0,
            audio_visual_conflict_score=_av.score(b"", None),  # UNAVAILABLE: #168
            grounding_confidence=(
                vision_sidecar.grounding_confidence()
                if vision_sidecar is not None
                else 0.0  # UNAVAILABLE: #161 — real grounding model pending model wiring
            ),
            deictic_ambiguous=False,
        )

    return _builder


def _make_policy_inputs_builder(
    vision_sidecar: Any,
    av_scorer: AudioVisualConflictScorer | None = None,
    urgency_scorer: UrgencyScorer | None = None,
) -> Callable[[TurnSignal, list[TurnSignal]], PolicyInputs]:
    """Backward-compatible factory: same as `_make_live_policy_inputs_builder`
    but without an `is_playing_fn` binding (assistant_speaking always False).

    Kept for the AV-conflict seam tests which exercise the builder in isolation
    without wiring an AudioOutputController.
    """
    return _make_live_policy_inputs_builder(
        is_playing_fn=None,
        vision_sidecar=vision_sidecar,
        av_scorer=av_scorer,
        urgency_scorer=urgency_scorer,
    )


# Module-level builder bound to no audio_output (assistant_speaking always
# False). Preserved for backward compatibility with tests that import the
# builder by name (e.g. tests/test_live_policy_inputs_builder_spec_aligned.py).
# Live runs always go through `_make_live_policy_inputs_builder(...)` in
# `build_live_pipeline` below with `is_playing_fn=audio_output.is_playing`.
_live_policy_inputs_builder = _make_live_policy_inputs_builder()


# ---------------------------------------------------------------------------
# LivePipeline
# ---------------------------------------------------------------------------


@dataclass
class LivePipeline:
    """Per-session live-loop state. Owns the orchestrator + audio_in queue."""

    session_id: str
    audio_in: asyncio.Queue[tuple[bytes, str]]
    orchestrator: StreamingRealtimeOrchestrator
    tts_adapter: Any = None
    vision_sidecar: Any = None
    session_state_store: Any = None
    core_store: Any = None
    episodic_store: Any = None
    semantic_store: Any = None

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
    asr_model: Any = None,
    use_stubs: bool = False,
    audio_out_broker: AudioOutSinkTarget | None = None,
    tts_adapter: Any = None,
    vision_sidecar: Any = None,
    config_store: ConfigStore | None = None,
    blob_dir: Path | None = None,
) -> LivePipeline:
    """Construct a LivePipeline for one ingest session.

    `foreground_duplex_model` must satisfy `StreamingDuplexModel` (it is wrapped
    in a `ForegroundModel`). At server startup the real instance is a singleton
    `MiniCPMStreamingModel`; tests inject a fake.

    `vad_model`, `smart_turn_model`, `backchannel_model`, `asr_model` are model
    instances satisfying the corresponding Protocols. They are typically
    pre-loaded singletons (e.g. SileroVADModel, PipecatSmartTurnModel,
    ASRLexiconBackchannelModel, FasterWhisperASRModel) constructed once at
    server startup. When `use_stubs=True` (or any individual model is None),
    the CPU stubs (EnergyVADModel / SilenceSmartTurnModel / ZeroBackchannelModel
    / EmptyTranscriptASRModel) are used in place of any missing model.
    Tests rely on this stubs-by-default behavior to avoid loading torch / ONNX
    / faster_whisper.

    `tts_adapter` is a `TtsAdapter` instance (typically a `KokoroTtsAdapter`
    singleton constructed once at server startup). When `use_stubs=True` or
    `tts_adapter is None`, a `NoopTtsAdapter` is used so tests and stub-mode
    runs do not load Kokoro. Voice-back fires only when this is a real adapter.

    The shared `logger` is wrapped in a `SharedLoggerProxy` so this session's
    StreamingRealtimeOrchestrator.stop() cannot shut down the Application-owned
    EventLogger on disconnect.

    `config_store` is the server-global Tier-B ConfigStore singleton (typically
    built once at server startup via :func:`build_config_store`). When provided,
    the orchestrator reads all 12 Tier-B keys at each EOU boundary and applies
    them to detectors, policy thresholds, and orchestrator timing (see
    docs/design-config-and-dashboard.md §3). When None, the orchestrator uses
    its construction-time defaults (backward compatible).
    """
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    shielded_logger = SharedLoggerProxy(logger)

    if use_stubs:
        vad_model = EnergyVADModel()
        smart_turn_model = SilenceSmartTurnModel()
        backchannel_model = ZeroBackchannelModel()
        tts_adapter = NoopTtsAdapter()
        asr_model = EmptyTranscriptASRModel()
    else:
        if vad_model is None:
            vad_model = EnergyVADModel()
        if smart_turn_model is None:
            smart_turn_model = SilenceSmartTurnModel()
        if backchannel_model is None:
            backchannel_model = ZeroBackchannelModel()
        if tts_adapter is None:
            tts_adapter = NoopTtsAdapter()
        if asr_model is None:
            asr_model = EmptyTranscriptASRModel()

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
    if audio_out_broker is not None:
        sink: Any = WebSocketAudioSink(session_id=session_id, broker=audio_out_broker)
    else:
        sink = _noop_audio_sink
    audio_output = AudioOutputController(
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
        sink=sink,
    )

    # MiniCPM-derived classifier is the final-product primary (issue #139).
    # WakeWordAddressingClassifier is the safety-net, active when MiniCPM returns None.
    # UNAVAILABLE: #157 — libcudart blocker, MiniCPM-derived addressing unavailable.
    minicpm_addressing = _NullMiniCPMAddressingClassifier()
    safety_net_addressing = WakeWordAddressingClassifier()

    # Bind the audio_output.is_playing callback into the builder closure so
    # `PolicyInputs.assistant_speaking` reflects live playback state. The
    # orchestrator-side signature is unchanged (closure approach).
    # Also bind the per-session `vision_sidecar` so the builder can source
    # `scene_change_score`, `grounding_confidence`, and (via the default
    # `_NullAudioVisualConflictScorer`) `audio_visual_conflict_score`.
    policy_inputs_builder = _make_live_policy_inputs_builder(
        is_playing_fn=lambda: audio_output.is_playing,
        vision_sidecar=vision_sidecar,
        urgency_scorer=_NullUrgencyScorer(),
    )

    session_state_store: Any = EmptyMemoryStore()
    core_store: Any = EmptyMemoryStore()
    episodic_store: Any = EmptyMemoryStore()
    semantic_store: Any = EmptyMemoryStore()
    if blob_dir is not None:
        mem_root = blob_dir / session_id / "memory"
        for subdir in ("session", "core", "episodic", "semantic"):
            (mem_root / subdir).mkdir(parents=True, exist_ok=True)

    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=shielded_logger,  # type: ignore[arg-type]
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=backchannel,
        policy_inputs_builder=policy_inputs_builder,
        foreground_model=foreground,
        audio_output=audio_output,
        tts_adapter=tts_adapter,
        proposal_batch_window_ms=proposal_batch_window_ms,
        decision_trace_dir=decision_trace_dir,
        asr_model=asr_model,
        vision_sidecar=vision_sidecar,
        minicpm_addressing_classifier=minicpm_addressing,
        addressing_classifier=safety_net_addressing,
        config_store=config_store,
        # UNAVAILABLE: #157 — libcudart blocker; null source routes every EOU
        # decision through SmartTurn/VAD fallback (signal_producer_fallback
        # event emitted per decision).
        native_duplex_eou_source=_NullNativeDuplexEouSource(),
        episodic_store=episodic_store,
        semantic_store=semantic_store,
    )

    return LivePipeline(
        session_id=session_id,
        audio_in=audio_in,
        orchestrator=orch,
        tts_adapter=tts_adapter,
        vision_sidecar=vision_sidecar,
        session_state_store=session_state_store,
        core_store=core_store,
        episodic_store=episodic_store,
        semantic_store=semantic_store,
    )
