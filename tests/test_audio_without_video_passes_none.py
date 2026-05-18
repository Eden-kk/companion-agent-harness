"""Audio-without-video path: video_bytes=None when no frame is buffered.

Plan: docs/plan-vision-sidecar-wiring.md Anchor 2.

Most audio chunks arrive without a paired video frame (cameras run at ~30 fps,
audio chunks at ~32 ms = ~30 fps too, but they're not aligned). The orchestrator
must yield (audio_bytes, None) in this case so the foreground model's audio
path is unaffected.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import PolicyInputs, ThinkerProposal, TurnSignal
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector
from companion_harness.vision_sidecar import VisionSidecar


class _Zero:
    def __call__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return 0.0


class _SmartTurnZero:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


class _NullSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0


class _NullGroundingModel:
    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return "", 0.0


class _StreamingFake:
    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[Any]) -> None:
        return None

    async def infer_stream(self, frame_iter, caused_by: list[str], context_items: tuple = ()):
        async def _gen():
            async for _ in frame_iter:
                pass
            return
            yield  # pragma: no cover

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


@pytest.mark.asyncio
async def test_audio_without_video_passes_none(tmp_path) -> None:
    async def _sink(_e):  # type: ignore[no-untyped-def]
        return None

    logger = EventLogger(_sink)
    sidecar = VisionSidecar(
        scene_scorer=_NullSceneScorer(),
        grounding_model=_NullGroundingModel(),
        session_id="test-session",
        logger=logger,
    )
    # First frame buffered, will fire once.
    sidecar.ingest_frame_bytes(b"frame-A", event_id="raw-video-A", timestamp_mono_ms=100)

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=8)
    ingest = InputIngest(logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")

    session_id = "test-session"
    vad = VADDetector(
        model=_Zero(),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(model=_SmartTurnZero(), session_id=session_id, logger=logger)
    bc = BackchannelClassifier(model=_Zero(), session_id=session_id, logger=logger)
    fg = ForegroundModel(model=_StreamingFake(), session_id=session_id, logger=logger)
    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink)

    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        vision_sidecar=sidecar,
    )

    # Push three audio chunks; only the first consumes the frame; remainder None.
    await orch._foreground_ring.put((b"chunk-1", "raw-audio-1"))
    await orch._foreground_ring.put((b"chunk-2", "raw-audio-2"))
    await orch._foreground_ring.put((b"chunk-3", "raw-audio-3"))

    gen = orch._bounded_frame_gen()
    out = [
        await asyncio.wait_for(gen.__anext__(), timeout=1.0),
        await asyncio.wait_for(gen.__anext__(), timeout=1.0),
        await asyncio.wait_for(gen.__anext__(), timeout=1.0),
    ]
    orch._batch_close_event.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    assert out[0] == (b"chunk-1", b"frame-A")
    assert out[1] == (b"chunk-2", None)
    assert out[2] == (b"chunk-3", None)
