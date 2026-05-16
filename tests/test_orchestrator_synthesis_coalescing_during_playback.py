"""F0a regression: only 1 synthesis dispatched when 2nd EOU arrives during playback.

Success criterion:
  - _synthesis_dispatch_task keeps _decision_in_flight=True until play_task completes
  - A 2nd TurnSignal arriving during TTS playback emits coalesced_during_playback (not a 2nd synthesis)
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


def _pcm(n: int = 512) -> bytes:
    return b"\x00" * n


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-coalesce",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


# ---------------------------------------------------------------------------
# Fake adapters
# ---------------------------------------------------------------------------


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
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            yield ThinkerProposal(
                proposal_type="observation",
                content="hello",
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="full_response",
                caused_by=caused_by,
            )

        return _gen()


class _AlwaysSpeakPolicy:
    """Returns full_response for every signal."""

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        return SpeakDecision(
            action_type="full_response",
            primary_reason_code=ReasonCode.EOU_CONFIRMED,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )


class _SlowTtsAdapter:
    """TTS that sleeps 300ms per chunk to simulate Kokoro playback duration."""

    def __init__(self) -> None:
        self.synthesize_call_count = 0

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        self.synthesize_call_count += 1
        # Simulate ~300ms of playback (10 chunks × 30ms each)
        for _ in range(10):
            await asyncio.sleep(0.03)
            yield b"\x00" * 160


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
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
        audio_visual_conflict_score=0.0,
        grounding_confidence=1.0,
        deictic_ambiguous=False,
    )


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    vad_probs: list[float],
    meta_offset: int = 0,
) -> None:
    for i, _ in enumerate(vad_probs):
        evt = ingest.ingest_chunk(session, _pcm(), _meta(meta_offset + i))
        await audio_in.put((_pcm(), evt.event_id))


def _build_orch(
    *,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    tts: _SlowTtsAdapter,
) -> StreamingRealtimeOrchestrator:
    sid = "s-coalesce"
    vad = VADDetector(
        model=_FakeVADModel(vad_probs),
        session_id=sid,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id=sid,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=_FakeBackchannelModel(),
        session_id=sid,
        logger=logger,
    )
    fg = ForegroundModel(
        model=_FakeStreamingModel(),
        session_id=sid,
        logger=logger,
    )
    controller = AudioOutputController(
        session_id=sid,
        logger=logger,
        sink=_noop_sink,
    )
    return StreamingRealtimeOrchestrator(
        session_id=sid,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=_AlwaysSpeakPolicy(),
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=tts,
        proposal_batch_window_ms=50,
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_one_synthesis_during_playback(tmp_path: Path):
    """A second EOU arriving ~100ms into a 300ms playback must NOT trigger a second synthesis.

    Asserts:
      - tts.synthesize_call_count == 1
      - at least one coalesced_during_playback event emitted
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=128)
    tts = _SlowTtsAdapter()

    # VAD script:
    #   Turn 1: 4 speech (0.9) → 3 silence (0.1) → EOU fires → full_response synthesis starts
    #   Turn 2: 4 speech (0.9) → 3 silence (0.1) → second EOU arrives during playback
    orch = _build_orch(
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1, 0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        tts=tts,
    )

    await orch.start()

    # Push turn 1 (speech → silence → EOU)
    await _push_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    # Wait for synthesis to start (~50ms: policy + proposal grace)
    await asyncio.sleep(0.15)
    # Push turn 2 frames during TTS playback (~100ms into 300ms playback)
    await _push_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1], meta_offset=7)
    # Wait for turn 1 playback to complete + settle
    await asyncio.sleep(0.5)

    await orch.stop()

    coalesce_events = [e for e in received if e.event_type == "coalesced_during_playback"]
    assert tts.synthesize_call_count == 1, (
        f"Expected 1 synthesis, got {tts.synthesize_call_count}; "
        f"events: {[e.event_type for e in received]}"
    )
    assert len(coalesce_events) >= 1, (
        f"Expected at least 1 coalesced_during_playback event; "
        f"events: {[e.event_type for e in received]}"
    )
