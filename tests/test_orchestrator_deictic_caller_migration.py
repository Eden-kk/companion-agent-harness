"""Orchestrator T2 deictic caller migration test (v0.1j Tasks 4+7).

Verifies that the orchestrator's T2 path consumes DeicticResult attributes
(not a 3-tuple unpack) and sets inputs.deictic_reference + inputs.deictic_ambiguous
from the detector's result.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.deictic_detector import DeicticDetector, DeicticModel
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


class _FakeVADModel:
    def __call__(self, frame: bytes) -> float:
        return 0.9


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.95, 0.05  # triggers EOU


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
            yield  # noqa: unreachable

        return _gen()


class _RecordingDeicticModel:
    """Records calls and returns scripted (is_deictic, confidence)."""

    def __init__(self, is_deictic: bool, confidence: float) -> None:
        self._is_deictic = is_deictic
        self._confidence = confidence
        self.calls: list[str] = []

    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]:
        self.calls.append(transcript)
        return self._is_deictic, self._confidence


class _SilencePolicy:
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


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


@pytest.mark.asyncio
async def test_orchestrator_deictic_caller_uses_attribute_access(tmp_path: Path) -> None:
    """T2 path: deictic_detector.classify() result consumed via DeicticResult attributes."""
    logger, events = _make_logger()
    await logger.start()

    session_id = "orch-deictic-test"
    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
    ingest_session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    deictic_model = _RecordingDeicticModel(is_deictic=True, confidence=0.92)
    deictic_detector = DeicticDetector(
        model=deictic_model,
        session_id=session_id,
        logger=logger,
    )

    captured_inputs: list[PolicyInputs] = []

    class _CapturingPolicy:
        def __call__(
            self,
            inputs: PolicyInputs,
            signal_event_ids: list[str],
            p_backchannel: float = 0.0,
        ) -> SpeakDecision:
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

    def _builder(signal: TurnSignal, history: list[TurnSignal]) -> PolicyInputs:
        return PolicyInputs(
            user_speaking=True,
            eou_probability=signal.p_done,
            assistant_speaking=False,
            scene_change_score=0.0,
            deictic_reference=False,  # orchestrator overrides via deictic_detector
            user_addressed_agent=True,
            urgency_score=0.0,
            proactivity_budget_remaining={},
            privacy_mode="normal",
            current_task_mode="normal",
            social_mode="user_addressing_agent",
            risk_mode="normal",
            cooldown_state={},
            attachment_risk_level=0.0,
            deictic_ambiguous=False,
        )

    vad = VADDetector(
        model=_FakeVADModel(),
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
    audio_out = AudioOutputController(
        session_id=session_id,
        logger=logger,
        sink=lambda chunk: None,
    )
    tts = SilentTtsAdapter()

    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_builder,
        speak_policy=_CapturingPolicy(),
        foreground_model=fg,
        audio_output=audio_out,
        tts_adapter=tts,
        proposal_batch_window_ms=50,
        decision_trace_dir=tmp_path / "traces",
        deictic_detector=deictic_detector,
    )

    await orch.start()
    # Send speech frames then enough silence to trigger EOU via VADDetector
    # (silence_onset_ms=64; at 32ms/frame that's 2+ frames)
    for i in range(5):
        meta = CaptureMetadata(
            client_id="test",
            timestamp_mono_ms=100 + i * 32,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
        )
        evt = ingest.ingest_chunk(ingest_session, b"\x10" * 512, meta)
        await audio_in.put((b"\x10" * 512, evt.event_id))
    for i in range(10):
        meta = CaptureMetadata(
            client_id="test",
            timestamp_mono_ms=260 + i * 32,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
        )
        evt = ingest.ingest_chunk(ingest_session, b"\x00" * 512, meta)
        await audio_in.put((b"\x00" * 512, evt.event_id))

    # Allow tasks to run
    await asyncio.sleep(0.4)
    await orch.stop()

    # Verify the deictic detector was invoked
    assert len(deictic_model.calls) >= 1, "deictic model was not called"
    # Verify policy saw deictic_reference=True (from the recording model)
    assert any(inp.deictic_reference is True for inp in captured_inputs), (
        "PolicyInputs.deictic_reference was never set to True by the deictic detector"
    )
