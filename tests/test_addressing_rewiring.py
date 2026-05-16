"""v0.1j Task 9 — Addressing producer routing contract tests.

MiniCPM-derived classifier is the final-product primary (issue #139).
WakeWordAddressingClassifier is the safety-net, active when MiniCPM returns None.
UNAVAILABLE: #157 — libcudart blocker, MiniCPM-derived addressing unavailable.

Tests:
  test_minicpm_classifier_preferred_when_available
      — MiniCPM-derived classifier returning explicit signal overrides WakeWord.
  test_falls_back_to_wake_word_when_minicpm_unavailable
      — _NullMiniCPMAddressingClassifier (None) → WakeWord fires, behavior preserved.
  test_signal_producer_fallback_event_for_addressing
      — fallback to WakeWord emits signal_producer_fallback event on the bus.
  test_null_minicpm_classifier_marker
      — UNAVAILABLE: #157 marker is present in addressing_classifier source.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

import companion_harness.addressing_classifier as _acmod
from companion_harness.addressing_classifier import (
    AddressingSignal,
    WakeWordAddressingClassifier,
    _NullMiniCPMAddressingClassifier,
)
from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
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
# Shared helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-rewiring",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


class _FakeVADModel:
    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def __call__(self, frame: bytes) -> float:
        return next(self._probs, 0.0)


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


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
            yield ThinkerProposal(
                proposal_type="observation",
                content="ok",
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="full_response",
                caused_by=caused_by,
            )

        return _gen()

    async def cancel_generation(self) -> None:
        pass


class _SpyPolicy:
    def __init__(self) -> None:
        self.captured_inputs: list[PolicyInputs] = []

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        self.captured_inputs.append(inputs)
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


class _ScriptedASRModel:
    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        return self._transcript


class _FakeMiniCPMClassifier:
    """Fake MiniCPM classifier that always returns a scripted AddressingSignal."""

    def __init__(self, signal: AddressingSignal) -> None:
        self._signal = signal

    def __call__(
        self,
        transcript: str,
        speaker_count: int | None,
        social_mode: str,
    ) -> AddressingSignal | None:
        return self._signal


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=signal.p_done <= signal.p_continue,
        eou_probability=signal.p_done,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        audio_visual_conflict_score=0.0,
        grounding_confidence=1.0,
        deictic_ambiguous=False,
    )


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    spy_policy: _SpyPolicy,
    asr_model=None,
    addressing_classifier=None,
    minicpm_addressing_classifier=None,
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
    controller = AudioOutputController(
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
        speak_policy=spy_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
        asr_model=asr_model,
        addressing_classifier=addressing_classifier,
        minicpm_addressing_classifier=minicpm_addressing_classifier,
    )


async def _push_frames(audio_in: asyncio.Queue, ingest: InputIngest, session, frame_count: int) -> None:
    for i in range(frame_count):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minicpm_classifier_preferred_when_available(tmp_path: Path) -> None:
    """MiniCPM-derived classifier returning explicit overrides WakeWord result.

    WakeWord would say implicit (no wake-word in transcript), but MiniCPM
    returns explicit. The orchestrator must use MiniCPM's signal and must NOT
    consult WakeWord when MiniCPM returns a non-None result.
    """
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-minicpm-preferred"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    spy = _SpyPolicy()
    minicpm = _FakeMiniCPMClassifier(AddressingSignal(confidence="explicit", evidence="minicpm_classifier"))
    # WakeWord would produce implicit (no wake-word in transcript below).
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        spy_policy=spy,
        asr_model=_ScriptedASRModel("tell me the time"),
        addressing_classifier=WakeWordAddressingClassifier(),
        minicpm_addressing_classifier=minicpm,
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    assert spy.captured_inputs, "decide() was never called"
    # MiniCPM returned explicit → user_addressed_agent must be True.
    assert spy.captured_inputs[-1].user_addressed_agent is True, (
        "MiniCPM classifier should be preferred; user_addressed_agent should be True"
    )


@pytest.mark.asyncio
async def test_falls_back_to_wake_word_when_minicpm_unavailable(tmp_path: Path) -> None:
    """_NullMiniCPMAddressingClassifier returns None → WakeWord safety-net fires.

    Behavior is identical to the pre-Task-9 3-tier path (PR #149 baseline).
    Wake-word in transcript → user_addressed_agent True.
    """
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-minicpm-unavailable"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    spy = _SpyPolicy()
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        spy_policy=spy,
        asr_model=_ScriptedASRModel("hey companion what time is it"),
        addressing_classifier=WakeWordAddressingClassifier(),
        minicpm_addressing_classifier=_NullMiniCPMAddressingClassifier(),
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    assert spy.captured_inputs, "decide() was never called"
    # WakeWord detects "companion" → user_addressed_agent must be True.
    assert spy.captured_inputs[-1].user_addressed_agent is True, (
        "WakeWord safety-net should fire when MiniCPM unavailable; wake-word present"
    )


@pytest.mark.asyncio
async def test_signal_producer_fallback_event_for_addressing(tmp_path: Path) -> None:
    """Fallback to WakeWord safety-net emits signal_producer_fallback event."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-fallback-event"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    spy = _SpyPolicy()
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        spy_policy=spy,
        asr_model=_ScriptedASRModel("hey companion"),
        addressing_classifier=WakeWordAddressingClassifier(),
        minicpm_addressing_classifier=_NullMiniCPMAddressingClassifier(),
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    fallback_evts = [e for e in received if e.event_type == "signal_producer_fallback"]
    assert fallback_evts, "signal_producer_fallback event must be emitted when WakeWord safety-net fires"
    evt = fallback_evts[0]
    assert evt.caused_by, "signal_producer_fallback must have caused_by[] (invariant #1)"


def test_null_minicpm_classifier_returns_none() -> None:
    """_NullMiniCPMAddressingClassifier always returns None (safety-net fallback contract)."""
    clf = _NullMiniCPMAddressingClassifier()
    result = clf("hey companion", speaker_count=None, social_mode="user_addressing_agent")
    assert result is None
