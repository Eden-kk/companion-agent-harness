"""policy_decision event and DecisionTrace tests (v0.1e Task 6 migration).

v0.1d had typed policy_decision_action_<X> sub-events (PR #91).
v0.1e Task 6 removes them; action_selected is now read from
DecisionTrace.counterfactuals["action_selected"] via the payload_ref URI.

Success criterion:
  pytest -k policy_decision_subevents passes with 3 tests.
  Every policy_decision event has a payload_ref pointing at a trace
  file that carries counterfactuals["action_selected"] == the action type.
  No policy_decision_action_* typed sub-events appear in the log.
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
from companion_harness.decision_trace_store import DecisionTraceStore
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
    trace_dir: Path | None = None,
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
        decision_trace_dir=trace_dir,
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
async def test_full_response_trace_has_action_selected(tmp_path: Path):
    """full_response decision: policy_decision.payload_ref points at a trace with
    counterfactuals['action_selected'] == 'full_response'.
    No typed sub-events emitted."""
    trace_dir = tmp_path / "decision_traces"
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
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
        trace_dir=trace_dir,
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, vad_probs)
    await asyncio.sleep(0.3)
    await orch.stop()

    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    assert policy_evts, "No policy_decision event emitted"

    # payload_ref must point at a trace file
    pd = policy_evts[0]
    assert pd.payload_ref is not None, "policy_decision.payload_ref is None"
    assert pd.payload_ref.startswith("decision_trace://")
    decision_id = pd.payload_ref[len("decision_trace://"):]

    store = DecisionTraceStore(trace_dir)
    trace = store.read(decision_id)
    assert trace.counterfactuals.get("action_selected") == "full_response", (
        f"action_selected={trace.counterfactuals.get('action_selected')!r}"
    )

    # No typed sub-events
    sub_evts = [e for e in received if e.event_type.startswith("policy_decision_action_")]
    assert not sub_evts, f"unexpected typed sub-events: {[e.event_type for e in sub_evts]}"


@pytest.mark.asyncio
async def test_silence_trace_has_action_selected(tmp_path: Path):
    """silence decision: policy_decision.payload_ref points at a trace with
    counterfactuals['action_selected'] == 'silence'."""
    trace_dir = tmp_path / "decision_traces"
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
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
        trace_dir=trace_dir,
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, vad_probs)
    await asyncio.sleep(0.3)
    await orch.stop()

    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    assert policy_evts, "No policy_decision event emitted"

    pd = policy_evts[0]
    assert pd.payload_ref is not None
    decision_id = pd.payload_ref[len("decision_trace://"):]

    store = DecisionTraceStore(trace_dir)
    trace = store.read(decision_id)
    assert trace.counterfactuals.get("action_selected") == "silence"

    # No typed sub-events
    sub_evts = [e for e in received if e.event_type.startswith("policy_decision_action_")]
    assert not sub_evts, f"unexpected typed sub-events: {[e.event_type for e in sub_evts]}"


@pytest.mark.asyncio
async def test_trace_count_matches_decision_count(tmp_path: Path):
    """Each policy_decision event has exactly one corresponding trace file on disk."""
    trace_dir = tmp_path / "decision_traces"
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
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
        trace_dir=trace_dir,
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, vad_probs)
    await asyncio.sleep(0.3)
    await orch.stop()

    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    assert policy_evts, "No policy_decision events emitted"

    store = DecisionTraceStore(trace_dir)
    for pd in policy_evts:
        assert pd.payload_ref is not None
        decision_id = pd.payload_ref[len("decision_trace://"):]
        trace = store.read(decision_id)
        assert trace.decision_id == decision_id

    # No typed sub-events
    sub_evts = [e for e in received if e.event_type.startswith("policy_decision_action_")]
    assert not sub_evts, (
        f"typed sub-events must not be emitted; found: {[e.event_type for e in sub_evts]}"
    )
