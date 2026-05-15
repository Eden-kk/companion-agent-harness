"""policy_decision_action_<X> typed sub-event emission tests (Task 7 prereq).

Success criterion:
  pytest -k policy_decision_subevents passes with 3 tests.
  Every SpeakDecision that produces a policy_decision event also
  co-emits a policy_decision_action_<action_type> sub-event whose
  caused_by list contains the parent policy_decision event_id.
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
        client_id="test-subevents",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


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
        return 0.1, 0.9  # never triggers EOU on its own


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

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
                content="scripted response",
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="full_response",
                caused_by=caused_by,
            )

        return _gen()


class _FixedActionPolicy:
    """Returns a SpeakDecision with a scripted action_type."""

    def __init__(self, action_type: str) -> None:
        self._action_type = action_type  # type: ignore[assignment]

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        return SpeakDecision(
            action_type=self._action_type,  # type: ignore[arg-type]
            primary_reason_code=ReasonCode.EOU_CONFIRMED,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )


# ---------------------------------------------------------------------------
# policy_inputs_builder
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Orchestrator builder
# ---------------------------------------------------------------------------


async def _noop_sink(chunk: bytes) -> None:
    pass


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    speak_policy=None,
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
        speak_policy=speak_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
    )


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    vad_probs: list[float],
) -> None:
    for i, _ in enumerate(vad_probs):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_response_emits_typed_subevent(tmp_path: Path):
    """full_response decision co-emits policy_decision_action_full_response
    whose caused_by contains the parent policy_decision event_id."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-subevt-full"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    vad_probs = [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        speak_policy=_FixedActionPolicy("full_response"),
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, vad_probs)
    await asyncio.sleep(0.3)
    await orch.stop()

    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    sub_evts = [e for e in received if e.event_type == "policy_decision_action_full_response"]

    assert policy_evts, "No policy_decision event emitted"
    assert sub_evts, "No policy_decision_action_full_response sub-event emitted"

    parent_id = policy_evts[0].event_id
    assert parent_id in sub_evts[0].caused_by, (
        f"Sub-event caused_by {sub_evts[0].caused_by!r} does not contain parent id {parent_id!r}"
    )
    assert isinstance(sub_evts[0].timestamp_mono_ms, int)


@pytest.mark.asyncio
async def test_silence_emits_typed_subevent(tmp_path: Path):
    """silence decision co-emits policy_decision_action_silence
    whose caused_by contains the parent policy_decision event_id."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-subevt-silence"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    vad_probs = [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        speak_policy=_FixedActionPolicy("silence"),
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, vad_probs)
    await asyncio.sleep(0.3)
    await orch.stop()

    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    sub_evts = [e for e in received if e.event_type == "policy_decision_action_silence"]

    assert policy_evts, "No policy_decision event emitted"
    assert sub_evts, "No policy_decision_action_silence sub-event emitted"

    parent_id = policy_evts[0].event_id
    assert parent_id in sub_evts[0].caused_by
    assert isinstance(sub_evts[0].timestamp_mono_ms, int)


@pytest.mark.asyncio
async def test_subevent_count_matches_decision_count(tmp_path: Path):
    """Each policy_decision event has exactly one corresponding typed sub-event."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-subevt-count"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    vad_probs = [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        speak_policy=_FixedActionPolicy("full_response"),
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, vad_probs)
    await asyncio.sleep(0.3)
    await orch.stop()

    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    sub_evts = [e for e in received if e.event_type.startswith("policy_decision_action_")]

    assert len(policy_evts) == len(sub_evts), (
        f"Mismatch: {len(policy_evts)} policy_decision events but "
        f"{len(sub_evts)} policy_decision_action_* sub-events"
    )
