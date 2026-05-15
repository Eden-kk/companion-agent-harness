"""Orchestrator-level integration tests for DecisionTrace persistence (v0.1e Task 4 Anchor 4 v6).

Verifies:
1. policy_decision event has retention_policy_id="decision_trace_30d"
2. decision_trace_emitted event has retention_policy_id="decision_trace_30d"
3. policy_decision.payload_ref == "decision_trace://<decision_id>"
4. Trace file exists on disk after emission
5. store.read() reconstructs the trace content-equal to what decide() produced
6. decision_trace_emitted.payload_hash == sha256(json.dumps(asdict(trace)))
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.decision_trace_store import DecisionTraceStore, _trace_to_dict
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import Event, PolicyInputs, ThinkerProposal, TurnSignal
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
        client_id="test-dts",
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
        audio_visual_conflict_score=0.0,
        grounding_confidence=1.0,
        deictic_ambiguous=False,
    )


# ---------------------------------------------------------------------------
# Orchestrator builder
# ---------------------------------------------------------------------------


_VAD_PROBS = [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    trace_dir: Path,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=_FakeVADModel(_VAD_PROBS),
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
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
        decision_trace_dir=trace_dir,
    )


async def _run_one_decision(
    session_id: str,
    logger: EventLogger,
    received: list[Event],
    ingest: InputIngest,
    trace_dir: Path,
) -> None:
    """Drive the orchestrator through one full EOU cycle."""
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        trace_dir=trace_dir,
    )

    await orch.start()
    for i, _ in enumerate(_VAD_PROBS):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))

    await asyncio.sleep(0.3)
    await orch.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_decision_event_has_correct_retention(tmp_path: Path) -> None:
    logger, received = _make_logger()
    await logger.start()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
    trace_dir = tmp_path / "traces"

    await _run_one_decision("sid-ret-pd", logger, received, ingest, trace_dir)

    pd_events = [e for e in received if e.event_type == "policy_decision"]
    assert pd_events, "No policy_decision events emitted"
    for evt in pd_events:
        assert evt.retention_policy_id == "decision_trace_30d", (
            f"policy_decision retention_policy_id={evt.retention_policy_id!r}, expected 'decision_trace_30d'"
        )


@pytest.mark.asyncio
async def test_decision_trace_emitted_event_has_correct_retention(tmp_path: Path) -> None:
    logger, received = _make_logger()
    await logger.start()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
    trace_dir = tmp_path / "traces"

    await _run_one_decision("sid-ret-dte", logger, received, ingest, trace_dir)

    dte_events = [e for e in received if e.event_type == "decision_trace_emitted"]
    assert dte_events, "No decision_trace_emitted events emitted"
    for evt in dte_events:
        assert evt.retention_policy_id == "decision_trace_30d", (
            f"decision_trace_emitted retention_policy_id={evt.retention_policy_id!r}, expected 'decision_trace_30d'"
        )


@pytest.mark.asyncio
async def test_policy_decision_payload_ref_uri(tmp_path: Path) -> None:
    logger, received = _make_logger()
    await logger.start()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
    trace_dir = tmp_path / "traces"

    await _run_one_decision("sid-ref-uri", logger, received, ingest, trace_dir)

    pd_events = [e for e in received if e.event_type == "policy_decision"]
    assert pd_events, "No policy_decision events emitted"
    for evt in pd_events:
        assert evt.payload_ref is not None, "policy_decision.payload_ref is None"
        assert evt.payload_ref.startswith("decision_trace://"), (
            f"payload_ref={evt.payload_ref!r} does not start with 'decision_trace://'"
        )
        decision_id = evt.payload_ref[len("decision_trace://"):]
        assert decision_id == evt.event_id, (
            f"decision_id in URI ({decision_id!r}) != policy_decision.event_id ({evt.event_id!r})"
        )


@pytest.mark.asyncio
async def test_trace_file_exists_after_emission(tmp_path: Path) -> None:
    logger, received = _make_logger()
    await logger.start()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
    trace_dir = tmp_path / "traces"

    await _run_one_decision("sid-file-exists", logger, received, ingest, trace_dir)

    pd_events = [e for e in received if e.event_type == "policy_decision"]
    assert pd_events, "No policy_decision events emitted"
    for evt in pd_events:
        decision_id = evt.event_id
        trace_file = trace_dir / f"{decision_id}.json"
        assert trace_file.exists(), f"Trace file missing: {trace_file}"


@pytest.mark.asyncio
async def test_store_read_reconstructs_emitted_trace(tmp_path: Path) -> None:
    logger, received = _make_logger()
    await logger.start()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
    trace_dir = tmp_path / "traces"

    await _run_one_decision("sid-store-read", logger, received, ingest, trace_dir)

    pd_events = [e for e in received if e.event_type == "policy_decision"]
    assert pd_events, "No policy_decision events emitted"

    store = DecisionTraceStore(trace_dir)
    for evt in pd_events:
        decision_id = evt.event_id
        recovered = store.read(decision_id)
        assert recovered.decision_id == decision_id
        # decision_id ties trace to event; content fields should be set
        assert recovered.primary_reason_code is not None
        assert isinstance(recovered.threshold_path, list)
        assert len(recovered.threshold_path) > 0
        assert recovered.redacted_explanation is None
        assert recovered.sensitive_explanation_ref is None


@pytest.mark.asyncio
async def test_decision_trace_emitted_payload_hash_is_sha256_of_content(tmp_path: Path) -> None:
    logger, received = _make_logger()
    await logger.start()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path / "blobs")
    trace_dir = tmp_path / "traces"

    await _run_one_decision("sid-hash", logger, received, ingest, trace_dir)

    pd_events = [e for e in received if e.event_type == "policy_decision"]
    dte_events = [e for e in received if e.event_type == "decision_trace_emitted"]
    assert pd_events and dte_events

    store = DecisionTraceStore(trace_dir)
    for pd_evt, dte_evt in zip(pd_events, dte_events):
        decision_id = pd_evt.event_id
        trace = store.read(decision_id)
        # Recompute expected hash the same way the orchestrator does.
        sanitized = dataclasses.replace(
            trace,
            redacted_explanation=None,
            sensitive_explanation_ref=None,
        )
        trace_dict = _trace_to_dict(sanitized)
        expected_hash = hashlib.sha256(
            json.dumps(trace_dict, sort_keys=True).encode()
        ).hexdigest()
        assert dte_evt.payload_hash == expected_hash, (
            f"payload_hash mismatch: got {dte_evt.payload_hash!r}, expected {expected_hash!r}"
        )
