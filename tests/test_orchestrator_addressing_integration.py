"""StreamingRealtimeOrchestrator + AddressingClassifier integration tests.

Verifies the orchestrator overrides `PolicyInputs.user_addressed_agent`
via the injected AddressingClassifier AFTER ASR and BEFORE
SpeakPolicy.decide(). Three scenarios:

  1. Wake-word in ASR transcript ⇒ classifier sets True.
  2. Empty transcript + mode=user_addressing_agent ⇒ mechanical fallback True.
  3. No classifier injected ⇒ orchestrator preserves builder's placeholder
     value (backward compatibility).

All scenarios are confirmed by inspecting the `PolicyInputs` instance
captured at the speak_policy boundary — that is the only place
`user_addressed_agent` is consulted by the policy layer.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.addressing_classifier import WakeWordAddressingClassifier
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
# Helpers
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
        client_id="test-addressing",
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
        return 0.1, 0.9  # never trips EOU on its own


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
    """StreamingDuplexModel that yields one scripted proposal per batch."""

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
    """Records the PolicyInputs instance passed to decide(); returns silence."""

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
    """ASRModel stub that returns a fixed transcript on every call."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        return self._transcript


def _build_policy_inputs(
    signal: TurnSignal, signal_history: list[TurnSignal]
) -> PolicyInputs:
    """Mirror manual_test_console.live_pipeline._live_policy_inputs_builder.

    user_addressed_agent=False is the placeholder; the orchestrator's
    AddressingClassifier overrides it post-ASR.
    """
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
    )


async def _push_frames(audio_in: asyncio.Queue, ingest: InputIngest, session, frame_count: int) -> None:
    for i in range(frame_count):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


# ---------------------------------------------------------------------------
# Test 1: wake-word in transcript ⇒ classifier overrides to True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifier_overrides_to_true_on_wake_word(tmp_path: Path):
    """ASR returns "hey companion ..." ⇒ orchestrator sets
    PolicyInputs.user_addressed_agent=True before calling decide(),
    even though the builder placeholder was False."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-addressing-wake"
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
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    assert spy.captured_inputs, "decide() was never called"
    # The classifier ran after ASR and BEFORE the policy sees PolicyInputs.
    assert spy.captured_inputs[0].user_addressed_agent is True


# ---------------------------------------------------------------------------
# Test 2: empty transcript + addressing-agent mode ⇒ mechanical fallback True
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_classifier_mechanical_fallback_addressing_agent(tmp_path: Path):
    """No wake-word, no diarization signal, social_mode=user_addressing_agent
    ⇒ classifier collapses to mode-default ⇒ True (matches pre-PR-#143 behavior)."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-addressing-mechanical"
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
        asr_model=_ScriptedASRModel(""),  # no wake-word
        addressing_classifier=WakeWordAddressingClassifier(),
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    assert spy.captured_inputs, "decide() was never called"
    assert spy.captured_inputs[0].user_addressed_agent is True


# ---------------------------------------------------------------------------
# Test 3: no classifier ⇒ orchestrator preserves builder placeholder
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_classifier_preserves_builder_value(tmp_path: Path):
    """When addressing_classifier is None, the orchestrator must not modify
    the value the builder produced (backward compatibility)."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-addressing-no-clf"
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
        addressing_classifier=None,  # explicitly disabled
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    assert spy.captured_inputs, "decide() was never called"
    # Builder placeholder was False; classifier disabled; placeholder must stand.
    assert spy.captured_inputs[0].user_addressed_agent is False
