"""v0.1j Task 11 — retrieval query populated from real ASR transcript.

Success criterion:
  pytest -k "memory_retrieve_called_with_transcript_not_empty_string or
             memory_retrieve_called_with_empty_when_no_transcript"
  Both tests pass.
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
from companion_harness.realtime_orchestrator import (
    RETRIEVAL_TOP_K,
    StreamingRealtimeOrchestrator,
)
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    Event,
    MemoryItem,
    PolicyInputs,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Fakes
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
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            return
            yield  # make it a generator

        return _gen()


class _SpyStore:
    """MemoryManager that records the query argument passed to retrieve()."""

    def __init__(self) -> None:
        self.retrieve_calls: list[tuple[str, int]] = []

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        pass

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        self.retrieve_calls.append((query, top_k))
        return []

    def forget(self, item_id: str) -> None:
        pass

    def hard_delete(self, item_id: str) -> None:
        pass


class _ScriptedASRModel:
    """ASRModel that returns a fixed transcript for any non-empty audio."""

    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        return self._transcript if audio_chunks else ""


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
        client_id="test-asr-query",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_sink(chunk: bytes) -> None:
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


def _silence_policy(
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


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    episodic_store=None,
    asr_model=None,
    tmp_path: Path,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=_FakeVADModel([0.9, 0.9, 0.9, 0.1, 0.1]),
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
        speak_policy=_silence_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
        decision_trace_dir=tmp_path / "traces",
        episodic_store=episodic_store,
        asr_model=asr_model,
    )


async def _run_orch_one_eou(
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_retrieve_called_with_transcript_not_empty_string(tmp_path: Path):
    """retrieve() receives the real ASR transcript, not an empty string."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    store = _SpyStore()
    asr = _ScriptedASRModel("remember to buy milk")

    orch = _build_orch(
        session_id="test-asr-query-nonempty",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        episodic_store=store,
        asr_model=asr,
        tmp_path=tmp_path,
    )

    await _run_orch_one_eou(orch, audio_in, ingest, session)

    assert store.retrieve_calls, "retrieve() was never called"
    query, top_k = store.retrieve_calls[0]
    assert query == "remember to buy milk", f"expected transcript as query, got {query!r}"
    assert top_k == RETRIEVAL_TOP_K

    # Payload must also carry the transcript, not empty string.
    mre_events = [e for e in received if e.event_type == "memory_retrieval_event"]
    assert mre_events
    payload = orch._memory_event_payloads.get(mre_events[0].event_id)
    assert payload is not None
    assert payload["query"] == "remember to buy milk"


@pytest.mark.asyncio
async def test_memory_retrieve_called_with_empty_when_no_transcript(tmp_path: Path):
    """With no ASR model wired, retrieve() receives query="" (no synthetic content)."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    store = _SpyStore()

    orch = _build_orch(
        session_id="test-asr-query-empty",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        episodic_store=store,
        asr_model=None,  # no ASR → transcript stays ""
        tmp_path=tmp_path,
    )

    await _run_orch_one_eou(orch, audio_in, ingest, session)

    assert store.retrieve_calls, "retrieve() was never called"
    query, _ = store.retrieve_calls[0]
    assert query == "", f"expected empty-string query when no ASR, got {query!r}"

    mre_events = [e for e in received if e.event_type == "memory_retrieval_event"]
    assert mre_events
    payload = orch._memory_event_payloads.get(mre_events[0].event_id)
    assert payload is not None
    assert payload["query"] == ""
