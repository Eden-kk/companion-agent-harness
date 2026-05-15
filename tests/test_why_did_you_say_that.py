"""test_why_did_you_say_that — v0.1e Task 18.

Spec line 544-545:
  test_why_did_you_say_that:
    expected: cites retrieval_used + policy_decision in trace

v0.1e limitation: retrieval is INERT — query="" produces 0 results because no
ASR transcript source is wired yet (Stage 5 ASR adapter deferred).  Tests focus
on wiring correctness and causal chain closure; a populated-retrieval scenario is
skipped with an explicit reason citing Stage 5 ASR.

Class A: real orchestrator, inert retrieval — 4 async tests.
Class B: spec scenario with injected retrieval — 1 test (skipped until Stage 5).
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
from companion_harness.foreground_model import DuplexModel, ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import (
    RETRIEVAL_TOP_K,
    StreamingRealtimeOrchestrator,
)
from companion_harness.schemas import (
    Event,
    MemoryItem,
    PolicyInputs,
    SensitiveField,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Fakes (minimal — mirroring test_cross_adapter_retrieval.py patterns)
# ---------------------------------------------------------------------------


class _FakeVADModel:
    def __init__(self) -> None:
        self._probs = iter([0.9, 0.9, 0.9, 0.1, 0.1])

    def __call__(self, frame: bytes) -> float:
        return next(self._probs, 0.0)


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
    def __init__(self) -> None:
        self.context_calls: list[list[MemoryItem]] = []

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        self.context_calls.append(list(items))

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


class _FakeStore:
    """Configurable retrieve results; records retrieve calls."""

    def __init__(self, items: list[MemoryItem] | None = None) -> None:
        self._items = items or []
        self.retrieve_calls: list[tuple[str, int]] = []
        self.committed: list[MemoryItem] = []

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        self.committed.append(item)

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        self.retrieve_calls.append((query, top_k))
        return self._items[:top_k]

    def forget(self, item_id: str) -> None:
        pass

    def hard_delete(self, item_id: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory_item(item_id: str = "item-001") -> MemoryItem:
    now = datetime.now(timezone.utc).isoformat()
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"text": "user likes hiking"},
        source_event_id="src-001",
        created_at=now,
        last_confirmed_at=now,
        confidence=0.9,
        salience=0.7,
        privacy_level="user_content",
        mutability="user_only",
        valid_from=now,
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value="user likes hiking",
        ),
    )


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _pcm_chunk() -> bytes:
    return b"\x00" * 512


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-wdyst",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_audio_sink(chunk: bytes) -> None:
    pass


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
        privacy_mode="normal",
        current_task_mode="default",
        social_mode="default",
        risk_mode="default",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    fake_model: _FakeStreamingModel | None = None,
    episodic_store=None,
    tmp_path: Path,
) -> StreamingRealtimeOrchestrator:
    model = fake_model or _FakeStreamingModel()
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
    fg = ForegroundModel(model=model, session_id=session_id, logger=logger)
    controller = AudioOutputController(
        session_id=session_id,
        logger=logger,
        sink=_noop_audio_sink,
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
        decision_trace_dir=tmp_path / "traces",
        episodic_store=episodic_store,
    )


async def _run_one_eou(
    orch: StreamingRealtimeOrchestrator,
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    n_frames: int = 7,
) -> None:
    await orch.start()
    for i in range(n_frames):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))
    await asyncio.sleep(0.3)
    await orch.stop()


# ---------------------------------------------------------------------------
# Class A: Wiring tests (real orchestrator, inert retrieval)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_retrieval_event_emitted_on_eou(tmp_path: Path):
    """One memory_retrieval_event fires per EOU with correct causal link to signal."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id="test-wdyst-mre",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        tmp_path=tmp_path,
    )
    await _run_one_eou(orch, audio_in, ingest, session)

    mre_events = [e for e in received if e.event_type == "memory_retrieval_event"]
    assert len(mre_events) >= 1

    mre = mre_events[0]
    assert mre.payload_kind == "memory_op"
    assert mre.subject_class == "self"
    assert mre.sensitivity == "safe"
    assert mre.retention_policy_id == "retrieval_audit_30d"
    # caused_by must include a signal event_id (non-empty)
    assert mre.caused_by, "memory_retrieval_event.caused_by is empty"


@pytest.mark.asyncio
async def test_decision_trace_retrieval_used_populated(tmp_path: Path):
    """DecisionTrace.retrieval_used contains the memory_retrieval_event id."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    trace_dir = tmp_path / "traces"
    orch = _build_orch(
        session_id="test-wdyst-trace",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        tmp_path=tmp_path,
    )
    await _run_one_eou(orch, audio_in, ingest, session)

    mre_events = [e for e in received if e.event_type == "memory_retrieval_event"]
    policy_events = [e for e in received if e.event_type == "policy_decision"]
    assert mre_events, "no memory_retrieval_event emitted"
    assert policy_events, "no policy_decision emitted"

    mre_id = mre_events[0].event_id
    policy_evt = policy_events[0]
    assert policy_evt.payload_ref and policy_evt.payload_ref.startswith("decision_trace://"), (
        f"policy_decision.payload_ref unexpected: {policy_evt.payload_ref!r}"
    )

    decision_id = policy_evt.payload_ref[len("decision_trace://"):]
    trace = DecisionTraceStore(trace_dir).read(decision_id)
    assert mre_id in trace.retrieval_used, (
        f"mre_id {mre_id!r} not in DecisionTrace.retrieval_used={trace.retrieval_used!r}"
    )


@pytest.mark.asyncio
async def test_policy_decision_caused_by_includes_mre(tmp_path: Path):
    """policy_decision.caused_by contains both the signal event_id and mre.event_id."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id="test-wdyst-caused",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        tmp_path=tmp_path,
    )
    await _run_one_eou(orch, audio_in, ingest, session)

    mre_events = [e for e in received if e.event_type == "memory_retrieval_event"]
    policy_events = [e for e in received if e.event_type == "policy_decision"]
    assert mre_events and policy_events

    mre_id = mre_events[0].event_id
    policy_evt = policy_events[0]

    assert mre_id in policy_evt.caused_by, (
        f"mre_id {mre_id!r} missing from policy_decision.caused_by={policy_evt.caused_by!r}"
    )
    assert len(policy_evt.caused_by) >= 2, (
        f"policy_decision.caused_by should have signal_id + mre_id, got {policy_evt.caused_by!r}"
    )


@pytest.mark.asyncio
async def test_causal_chain_closes_full_path(tmp_path: Path):
    """Walk full chain: signal → mre → policy_decision → decision_trace_emitted."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id="test-wdyst-chain",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        tmp_path=tmp_path,
    )
    await _run_one_eou(orch, audio_in, ingest, session)

    by_id = {e.event_id: e for e in received}

    mre_events = [e for e in received if e.event_type == "memory_retrieval_event"]
    policy_events = [e for e in received if e.event_type == "policy_decision"]
    trace_events = [e for e in received if e.event_type == "decision_trace_emitted"]
    assert mre_events and policy_events and trace_events

    mre = mre_events[0]
    policy_evt = policy_events[0]
    trace_evt = trace_events[0]

    # signal → mre: mre.caused_by contains a real event_id
    assert mre.caused_by, "mre.caused_by is empty"
    signal_id = mre.caused_by[0]
    assert signal_id in by_id, f"signal event {signal_id!r} not found in received events"

    # mre → policy_decision: policy.caused_by contains mre.event_id
    assert mre.event_id in policy_evt.caused_by

    # policy_decision → decision_trace_emitted: trace.caused_by contains policy.event_id
    assert policy_evt.event_id in trace_evt.caused_by, (
        f"policy_evt.event_id {policy_evt.event_id!r} not in "
        f"decision_trace_emitted.caused_by={trace_evt.caused_by!r}"
    )


# ---------------------------------------------------------------------------
# Class B: Spec scenario (injected retrieval)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spec_scenario_with_injected_retrieval(tmp_path: Path):
    """Spec line 544-545: trace cites retrieval_used + policy_decision.

    With a pre-populated episodic store the trace should list the MRE event_id
    in retrieval_used AND the policy_decision event should point at the trace.
    Retrieval item content appears in DecisionTrace.retrieval_used (event provenance),
    not in the trace text itself.

    Note: query="" because ASR transcript source is not wired until Stage 5.
    Items ARE returned (store is pre-populated), so retrieval_used is non-empty.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    item = _make_memory_item("item-injected-001")
    store = _FakeStore([item])

    trace_dir = tmp_path / "traces"
    orch = _build_orch(
        session_id="test-wdyst-spec",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        episodic_store=store,
        tmp_path=tmp_path,
    )
    await _run_one_eou(orch, audio_in, ingest, session)

    mre_events = [e for e in received if e.event_type == "memory_retrieval_event"]
    policy_events = [e for e in received if e.event_type == "policy_decision"]
    assert mre_events and policy_events

    mre_id = mre_events[0].event_id
    policy_evt = policy_events[0]

    # Spec assertion: trace cites retrieval_used + policy_decision
    decision_id = policy_evt.payload_ref[len("decision_trace://"):]  # type: ignore[index]
    trace = DecisionTraceStore(trace_dir).read(decision_id)

    assert mre_id in trace.retrieval_used, (
        "spec scenario: trace.retrieval_used does not cite the memory_retrieval_event"
    )
    assert policy_evt.payload_ref and "decision_trace://" in policy_evt.payload_ref, (
        "spec scenario: policy_decision.payload_ref does not point at decision_trace"
    )
    # Retrieval payload records the item was returned (result_count >= 1)
    payload = orch._memory_event_payloads.get(mre_id)
    assert payload is not None
    assert payload["result_count"] >= 1, (
        "spec scenario: pre-populated store should yield result_count >= 1 "
        "(query='' because ASR not wired yet — Stage 5 ASR adapter will enable content retrieval)"
    )
