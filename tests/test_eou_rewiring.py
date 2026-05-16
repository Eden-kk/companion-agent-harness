"""EOU producer routing — v0.1j Task 8.

Success criterion:
    pytest -k "eou_rewiring" passes (6 tests).

Covers:
  - _NullNativeDuplexEouSource carries the UNAVAILABLE #157 marker
  - _NullNativeDuplexEouSource satisfies NativeDuplexEouSource Protocol
  - native_duplex preferred when source returns a TurnSignal
  - fallback to SmartTurn/VAD when native_duplex returns None
  - signal_producer_fallback event emitted with closed caused_by on fallback
  - REGRESSION GUARD: _snapshot_config() still runs in _policy_gate_task
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.native_duplex_eou import NativeDuplexEouSource, _NullNativeDuplexEouSource
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


def _pcm_speech(n_bytes: int = 512) -> bytes:
    return b"\x10" * n_bytes


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-eou",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


class _FakeVADModel:
    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def __call__(self, frame: bytes) -> float:
        return next(self._probs, 0.0)


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.9, 0.1  # always triggers EOU (p_done > p_continue)


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items) -> None:
        pass

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            return
            yield  # make it a generator

        return _gen()


class _AlwaysSilencePolicy:
    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        return SpeakDecision(
            action_type="silence",
            primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=signal.p_done <= signal.p_continue,
        eou_probability=signal.p_done,
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
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    native_duplex_eou_source=None,
    speak_policy=None,
    trace_dir: Path,
    config_store=None,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=_FakeVADModel(vad_probs),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=_FakeBackchannelModel(),
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(
        model=_FakeStreamingModel(),
        session_id=session_id,
        logger=logger,
    )
    aoc = AudioOutputController(
        session_id=session_id,
        logger=logger,
        sink=_noop_sink,
    )
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=speak_policy or _AlwaysSilencePolicy(),
        foreground_model=fg,
        audio_output=aoc,
        tts_adapter=SilentTtsAdapter(),
        proposal_batch_window_ms=50,
        decision_trace_dir=trace_dir,
        native_duplex_eou_source=native_duplex_eou_source,
        config_store=config_store,
    )


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    chunks: list[bytes],
) -> None:
    for i, chunk in enumerate(chunks):
        evt = ingest.ingest_chunk(session, chunk, _meta(i))
        await audio_in.put((chunk, evt.event_id))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_null_native_duplex_source_marker():
    """_NullNativeDuplexEouSource source must carry the UNAVAILABLE: #157 marker."""
    src = _NullNativeDuplexEouSource()
    source_text = inspect.getsource(src.__class__)
    assert "UNAVAILABLE: #157" in source_text
    assert src.get_eou_signal() is None


def test_native_duplex_eou_source_protocol():
    """_NullNativeDuplexEouSource satisfies NativeDuplexEouSource Protocol."""
    assert isinstance(_NullNativeDuplexEouSource(), NativeDuplexEouSource)


@pytest.mark.asyncio
async def test_native_duplex_preferred_when_available(tmp_path: Path):
    """When NativeDuplexEouSource returns a TurnSignal, it overrides the SmartTurn signal.

    The orchestrator must use the native_duplex signal's p_done value for the
    EOU decision (reflected in eou_probability fed to policy).
    """
    logger, received = _make_logger()
    await logger.start()

    native_signal = TurnSignal(
        detector="native_duplex",
        p_done=0.97,
        p_continue=0.03,
        p_backchannel=0.0,
        confidence=0.97,
        evidence_event_ids=["native-evt-001"],
    )

    class _FakeNativeDuplexSource:
        def __init__(self) -> None:
            self._calls = 0

        def get_eou_signal(self) -> TurnSignal | None:
            self._calls += 1
            return native_signal

    native_source = _FakeNativeDuplexSource()
    captured_inputs: list[PolicyInputs] = []

    class _CapturingPolicy:
        def __call__(self, inputs, signal_event_ids, p_backchannel=0.0):
            captured_inputs.append(inputs)
            return SpeakDecision(
                action_type="silence",
                primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
                supporting_reason_codes=[],
                redacted_explanation=None,
                caused_by=list(signal_event_ids),
                budget_bucket=None,
                allowed_prosody_tags=[],
                max_duration_ms=None,
            )

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-eou-nd")

    orch = _build_orch(
        session_id="eou-test-nd",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9] * 5 + [0.0] * 5,
        native_duplex_eou_source=native_source,
        speak_policy=_CapturingPolicy(),
        trace_dir=tmp_path,
    )

    await orch.start()

    # Speech frames then silence to trigger SmartTurn; native_duplex overrides it
    chunks = [_pcm_speech()] * 5 + [_pcm_chunk()] * 5
    await _push_frames(audio_in, ingest, session, chunks)

    await asyncio.sleep(0.15)
    await orch.stop()

    # native_duplex source was consulted on at least one EOU decision
    assert native_source._calls >= 1

    # When native_duplex returned a signal, policy saw its p_done (0.97)
    native_driven = [inp for inp in captured_inputs if inp.eou_probability == pytest.approx(0.97)]
    assert native_driven, "expected at least one policy call with native_duplex p_done=0.97"

    # No signal_producer_fallback event when native_duplex succeeds
    fallback_events = [e for e in received if e.event_type == "signal_producer_fallback"]
    # Filter to EOU fallback only (not addressing fallback from Task 9)
    eou_fallback = [e for e in fallback_events if e.payload_inline and e.payload_inline.get("primary_producer") == "native_duplex"]
    assert eou_fallback == [], "expected no EOU signal_producer_fallback when native_duplex available"


@pytest.mark.asyncio
async def test_falls_back_to_smartturn_when_native_unavailable(tmp_path: Path):
    """When NativeDuplexEouSource returns None, SmartTurn/VAD signal is used."""
    logger, received = _make_logger()
    await logger.start()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-eou-fb")

    orch = _build_orch(
        session_id="eou-test-fb",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9] * 5 + [0.0] * 5,
        native_duplex_eou_source=_NullNativeDuplexEouSource(),
        trace_dir=tmp_path,
    )

    await orch.start()

    chunks = [_pcm_speech()] * 5 + [_pcm_chunk()] * 5
    await _push_frames(audio_in, ingest, session, chunks)

    await asyncio.sleep(0.15)
    await orch.stop()

    # signal_producer_fallback must have been emitted
    eou_fallback = [
        e for e in received
        if e.event_type == "signal_producer_fallback"
        and e.payload_inline is not None
        and e.payload_inline.get("primary_producer") == "native_duplex"
    ]
    assert eou_fallback, "expected signal_producer_fallback event when native_duplex unavailable"


@pytest.mark.asyncio
async def test_signal_producer_fallback_event_emitted_with_caused_by(tmp_path: Path):
    """signal_producer_fallback event must have non-empty caused_by (invariant #1)."""
    logger, received = _make_logger()
    await logger.start()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-eou-cb")

    orch = _build_orch(
        session_id="eou-test-cb",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9] * 5 + [0.0] * 5,
        native_duplex_eou_source=_NullNativeDuplexEouSource(),
        trace_dir=tmp_path,
    )

    await orch.start()

    chunks = [_pcm_speech()] * 5 + [_pcm_chunk()] * 5
    await _push_frames(audio_in, ingest, session, chunks)

    await asyncio.sleep(0.15)
    await orch.stop()

    eou_fallback = [
        e for e in received
        if e.event_type == "signal_producer_fallback"
        and e.payload_inline is not None
        and e.payload_inline.get("primary_producer") == "native_duplex"
    ]
    assert eou_fallback, "expected at least one EOU signal_producer_fallback event"

    for evt in eou_fallback:
        assert evt.caused_by, (
            f"orphan signal_producer_fallback event {evt.event_id!r} — caused_by is empty"
        )
        assert evt.payload_inline["fallback_producer"] in ("vad", "smart_turn", "backchannel")
        assert evt.payload_inline["reason"] == "native_duplex_unavailable"


@pytest.mark.asyncio
async def test_existing_config_store_wiring_intact(tmp_path: Path):
    """REGRESSION GUARD (PR #152): _snapshot_config() still runs in _policy_gate_task.

    Wires a ConfigStore stub that counts get() calls. On at least one EOU
    decision, the orchestrator must read all 12 Tier-B keys. This guards
    against silent reverts of the Tier-B wiring.
    """
    logger, _received = _make_logger()
    await logger.start()

    class _CountingConfigStore:
        """Stub ConfigStore returning safe defaults; counts reads per key."""

        def __init__(self) -> None:
            self.calls: list[str] = []
            self._defaults = {
                "orchestrator.proposal_batch_window_ms": 80,
                "orchestrator.hard_cancel_after_ms": 120,
                "orchestrator.p_speech_thresh": 0.5,
                "orchestrator.p_backchannel_thresh": 0.7,
                "policy.backchannel_threshold": 0.7,
                "policy.audio_visual_conflict_threshold": 0.6,
                "policy.grounding_confidence_threshold": 0.5,
                "detectors.vad.speech_threshold": 0.5,
                "detectors.vad.silence_onset_ms": 320,
                "detectors.smart_turn.silence_onset_ms": 320,
                "detectors.smart_turn.silence_rms_threshold": 0.02,
                "detectors.backchannel.emit_threshold": 0.7,
            }

        def get(self, key: str):
            self.calls.append(key)
            return self._defaults[key]

    cfg = _CountingConfigStore()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-eou-cfg")

    orch = _build_orch(
        session_id="eou-test-cfg",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9] * 5 + [0.0] * 5,
        native_duplex_eou_source=_NullNativeDuplexEouSource(),
        trace_dir=tmp_path,
        config_store=cfg,
    )

    await orch.start()

    chunks = [_pcm_speech()] * 5 + [_pcm_chunk()] * 5
    await _push_frames(audio_in, ingest, session, chunks)

    await asyncio.sleep(0.15)
    await orch.stop()

    # All 12 Tier-B keys must have been read at least once.
    expected_keys = {
        "orchestrator.proposal_batch_window_ms",
        "orchestrator.hard_cancel_after_ms",
        "orchestrator.p_speech_thresh",
        "orchestrator.p_backchannel_thresh",
        "policy.backchannel_threshold",
        "policy.audio_visual_conflict_threshold",
        "policy.grounding_confidence_threshold",
        "detectors.vad.speech_threshold",
        "detectors.vad.silence_onset_ms",
        "detectors.smart_turn.silence_onset_ms",
        "detectors.smart_turn.silence_rms_threshold",
        "detectors.backchannel.emit_threshold",
    }
    seen = set(cfg.calls)
    missing = expected_keys - seen
    assert not missing, (
        f"REGRESSION GUARD FAILED: _snapshot_config() did not read all 12 "
        f"Tier-B keys at EOU. Missing: {sorted(missing)}"
    )
