"""Wiring contract: VisionSidecar → orchestrator → foreground (audio,video) tuple.

Plan: docs/plan-vision-sidecar-wiring.md BLOCKER-2 + Anchor 1.

Drives `_bounded_frame_gen` through a `StreamingRealtimeOrchestrator` constructed
with a VisionSidecar. Pre-loads a pending frame into the sidecar, pushes one
audio chunk, and asserts the generator yields `(audio_bytes, video_bytes)` with
both populated. No torch / MiniCPM dependency.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector
from companion_harness.vision_sidecar import VisionSidecar


class _FakeVAD:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeSmartTurn:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


class _FakeBackchannel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _NullSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0


class _NullGroundingModel:
    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return "", 0.0


class _CapturingStreamingModel:
    """StreamingDuplexModel fake — records every (audio,video) tuple it sees."""

    def __init__(self) -> None:
        self.captured: list[tuple[bytes, bytes | None]] = []

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[Any]) -> None:
        return None

    async def infer_stream(self, frame_iter, caused_by: list[str]):
        captured = self.captured

        async def _gen():
            async for audio_bytes, video_bytes in frame_iter:
                captured.append((audio_bytes, video_bytes))
            return
            yield  # pragma: no cover — make it a generator

        return _gen()


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
        eou_probability=0.0,
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


async def _noop_sink(chunk: bytes) -> None:
    return None


def _make_sidecar(logger: EventLogger) -> VisionSidecar:
    return VisionSidecar(
        scene_scorer=_NullSceneScorer(),
        grounding_model=_NullGroundingModel(),
        session_id="test-session",
        logger=logger,
    )


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    streaming_model: Any,
    vision_sidecar: VisionSidecar | None,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=_FakeVAD(),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=_FakeSmartTurn(),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=_FakeBackchannel(),
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(
        model=streaming_model,
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
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        vision_sidecar=vision_sidecar,
    )


def test_consume_video_or_none_returns_buffered_bytes(tmp_path) -> None:
    """Synchronous unit test for orchestrator._consume_video_or_none()."""
    async def _sink(_e: Event) -> None:
        return None

    logger = EventLogger(_sink)
    sidecar = _make_sidecar(logger)
    sidecar.ingest_frame_bytes(b"jpeg-payload", event_id="raw-video-1", timestamp_mono_ms=100)

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=8)
    ingest = InputIngest(logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    orch = _build_orch(
        session_id="test-session",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        streaming_model=_CapturingStreamingModel(),
        vision_sidecar=sidecar,
    )
    assert orch._consume_video_or_none() == b"jpeg-payload"
    assert orch._consume_video_or_none() is None


def test_consume_video_or_none_without_sidecar_returns_none(tmp_path) -> None:
    async def _sink(_e: Event) -> None:
        return None

    logger = EventLogger(_sink)
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=8)
    ingest = InputIngest(logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    orch = _build_orch(
        session_id="test-session",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        streaming_model=_CapturingStreamingModel(),
        vision_sidecar=None,
    )
    assert orch._consume_video_or_none() is None


@pytest.mark.asyncio
async def test_bounded_frame_gen_pairs_audio_with_video(tmp_path) -> None:
    """Drive _bounded_frame_gen end-to-end: pre-load a frame, push an audio chunk,
    assert the generator yields (audio, video) with both populated."""
    async def _sink(_e: Event) -> None:
        return None

    logger = EventLogger(_sink)
    sidecar = _make_sidecar(logger)
    sidecar.ingest_frame_bytes(b"jpeg-payload", event_id="raw-video-1", timestamp_mono_ms=100)

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=8)
    ingest = InputIngest(logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    orch = _build_orch(
        session_id="test-session",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        streaming_model=_CapturingStreamingModel(),
        vision_sidecar=sidecar,
    )

    # Stage an audio frame onto _tee_to_foreground (bypass the audio_tee task
    # so this test stays synchronous-ish).
    await orch._tee_to_foreground.put((b"audio-chunk-1", "raw-audio-evt-1"))

    # Start the generator; it should yield (audio, video) then block waiting
    # for more frames. Close the batch after the first yield.
    gen = orch._bounded_frame_gen()
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    audio_bytes, video_bytes = first
    assert audio_bytes == b"audio-chunk-1"
    assert video_bytes == b"jpeg-payload"

    # Close the batch to terminate the generator.
    orch._batch_close_event.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=1.0)


@pytest.mark.asyncio
async def test_bounded_frame_gen_passes_none_when_no_video(tmp_path) -> None:
    """When no frame is buffered, the generator must still yield (audio, None)."""
    async def _sink(_e: Event) -> None:
        return None

    logger = EventLogger(_sink)
    sidecar = _make_sidecar(logger)
    # No ingest_frame_bytes call — buffer empty.

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=8)
    ingest = InputIngest(logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    orch = _build_orch(
        session_id="test-session",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        streaming_model=_CapturingStreamingModel(),
        vision_sidecar=sidecar,
    )

    await orch._tee_to_foreground.put((b"audio-chunk-1", "raw-audio-evt-1"))

    gen = orch._bounded_frame_gen()
    first = await asyncio.wait_for(gen.__anext__(), timeout=1.0)
    assert first == (b"audio-chunk-1", None)

    orch._batch_close_event.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=1.0)
