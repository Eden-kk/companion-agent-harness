"""ASR transcript audit event contract tests — invariant #1 P0 fix (PR #144 review).

Success criterion (verbatim):
  pytest -k asr_transcript_event passes non-vacuously.
  - test_transcript_event_emitted_on_eou_with_text: a non-empty fake ASR
    output produces exactly one `asr_transcript_emitted` event in the log
    with the expected payload + caused_by chain.
  - test_empty_transcript_no_event: empty fake ASR output → no
    asr_transcript_emitted event in the log.
  - test_no_asr_adapter_no_event: asr_model=None → no asr_transcript_emitted
    event in the log.
  - test_decision_trace_references_transcript_event: DecisionTrace
    .signal_event_ids contains the transcript event's event_id.
  - test_causal_chain_closes_with_transcript_event: orphan_count == 0 with
    the new event in the stream.
  - test_transcript_event_passes_v0_1f_schema: the emitted event's
    classification axes match v0_1f_event_schema.EVENT_TYPE_SCHEMAS
    ["asr_transcript_emitted"].

Design choice: when transcript is the empty string OR when no ASR adapter
is wired, NO event is emitted.  Rationale: invariant #1 covers signals
that drive behavior; an empty transcript drives nothing (both
_detect_explicit_remember and the addressing classifier no-op on it).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.asr_adapter import ASRModel
from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.causal_graph import CausalGraph
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    SensitiveField,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.reason_codes import ReasonCode
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector
from companion_harness.v0_1f_event_schema import EVENT_TYPE_SCHEMAS


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/test_asr_adapter.py)
# ---------------------------------------------------------------------------


class _FakeASRModel:
    """Returns scripted transcripts in order. Optional `label` attribute."""

    def __init__(self, scripted: list[str], label: str | None = None) -> None:
        self._scripted = list(scripted)
        self._call_count = 0
        if label is not None:
            self.label = label

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        if self._call_count >= len(self._scripted):
            self._call_count += 1
            return ""
        out = self._scripted[self._call_count]
        self._call_count += 1
        return out


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
                content="scripted",
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="full_response",
                caused_by=caused_by,
            )

        return _gen()


class _SilencePolicy:
    """Returns silence so the TTS path does not execute."""

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
        client_id="test-asr-event",
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


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    asr_model: ASRModel | None,
    tmp_path: Path,
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
        speak_policy=_SilencePolicy(),
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=50,
        decision_trace_dir=tmp_path / "decision_traces",
        asr_model=asr_model,
    )


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    frame_count: int,
    start_i: int = 0,
) -> None:
    for i in range(frame_count):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(start_i + i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


async def _run_until_eou(
    *,
    asr_model: ASRModel | None,
    tmp_path: Path,
    session_id: str,
) -> tuple[list[Event], StreamingRealtimeOrchestrator]:
    logger, received = _make_logger()
    await logger.start()
    try:
        ingest = InputIngest(logger=logger, blob_dir=tmp_path)
        session = ingest.open_session("test-client")
        audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
        orch = _build_orch(
            session_id=session_id,
            logger=logger,
            ingest_session=session,
            audio_in=audio_in,
            vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
            asr_model=asr_model,
            tmp_path=tmp_path,
        )
        await orch.start()
        await _push_frames(audio_in, ingest, session, 7)
        await asyncio.sleep(0.3)
        await orch.stop()
    finally:
        try:
            await logger.stop()
        except Exception:
            pass
    return received, orch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transcript_event_emitted_on_eou_with_text(tmp_path: Path) -> None:
    """Non-empty fake ASR → exactly one asr_transcript_emitted event with
    the expected payload fields + caused_by closure to the signal event."""
    fake_asr = _FakeASRModel(["hello world"], label="fake-whisper")
    received, _ = await _run_until_eou(
        asr_model=fake_asr,
        tmp_path=tmp_path,
        session_id="test-asr-event-emit",
    )

    transcript_evts = [e for e in received if e.event_type == "asr_transcript_emitted"]
    assert len(transcript_evts) == 1, (
        f"Expected exactly 1 asr_transcript_emitted event, got {len(transcript_evts)}. "
        f"All event types: {[e.event_type for e in received]}"
    )
    evt = transcript_evts[0]

    # Classification axes match the v0.1f schema.
    schema = EVENT_TYPE_SCHEMAS["asr_transcript_emitted"]
    assert evt.payload_kind == schema.payload_kind == "transcript"
    assert evt.subject_class == schema.subject_class == "self"
    assert evt.sensitivity == schema.sensitivity == "sensitive"
    assert evt.retention_policy_id == schema.retention_policy_id == "transcript_audit_30d"

    # caused_by closes through some preceding event (the TurnSignal evidence).
    assert evt.caused_by, "asr_transcript_emitted has empty caused_by[] — orphan"
    known_ids = {e.event_id for e in received}
    for ref in evt.caused_by:
        assert ref in known_ids, f"asr_transcript_emitted caused_by ref {ref!r} not resolvable"


@pytest.mark.asyncio
async def test_empty_transcript_no_event(tmp_path: Path) -> None:
    """Empty fake ASR output → no asr_transcript_emitted event."""
    fake_asr = _FakeASRModel([""])
    received, _ = await _run_until_eou(
        asr_model=fake_asr,
        tmp_path=tmp_path,
        session_id="test-asr-event-empty",
    )
    transcript_evts = [e for e in received if e.event_type == "asr_transcript_emitted"]
    assert transcript_evts == [], (
        f"Expected no asr_transcript_emitted events for empty transcript, "
        f"got {len(transcript_evts)}"
    )


@pytest.mark.asyncio
async def test_no_asr_adapter_no_event(tmp_path: Path) -> None:
    """asr_model=None → no asr_transcript_emitted event."""
    received, _ = await _run_until_eou(
        asr_model=None,
        tmp_path=tmp_path,
        session_id="test-asr-event-none",
    )
    transcript_evts = [e for e in received if e.event_type == "asr_transcript_emitted"]
    assert transcript_evts == [], (
        f"Expected no asr_transcript_emitted events with asr_model=None, "
        f"got {len(transcript_evts)}"
    )


@pytest.mark.asyncio
async def test_decision_trace_references_transcript_event(tmp_path: Path) -> None:
    """DecisionTrace.signal_event_ids contains the transcript event_id."""
    fake_asr = _FakeASRModel(["hello world"])
    received, _ = await _run_until_eou(
        asr_model=fake_asr,
        tmp_path=tmp_path,
        session_id="test-asr-event-trace",
    )

    transcript_evts = [e for e in received if e.event_type == "asr_transcript_emitted"]
    assert len(transcript_evts) == 1
    transcript_evt_id = transcript_evts[0].event_id

    # Find the policy_decision event and load its trace.
    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    assert policy_evts, "No policy_decision event found"
    policy_evt = policy_evts[0]
    assert policy_evt.payload_ref is not None
    # decision_trace://<decision_id>
    decision_id = policy_evt.payload_ref.split("://", 1)[1]

    trace_path = tmp_path / "decision_traces" / f"{decision_id}.json"
    assert trace_path.exists(), f"DecisionTrace file missing: {trace_path}"
    trace_data = json.loads(trace_path.read_text())
    assert transcript_evt_id in trace_data["signal_event_ids"], (
        f"DecisionTrace.signal_event_ids does not contain transcript event "
        f"{transcript_evt_id!r}. signal_event_ids={trace_data['signal_event_ids']!r}"
    )


@pytest.mark.asyncio
async def test_causal_chain_closes_with_transcript_event(tmp_path: Path) -> None:
    """orphan_count == 0 after emitting asr_transcript_emitted."""
    fake_asr = _FakeASRModel(["hello world"])
    received, _ = await _run_until_eou(
        asr_model=fake_asr,
        tmp_path=tmp_path,
        session_id="test-asr-event-causal",
    )
    graph = CausalGraph(received)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"Causal graph has orphan events after asr_transcript_emitted: "
        f"{report.dangling_refs}"
    )


def test_transcript_event_schema_registered() -> None:
    """The new event type is registered in the v0.1f schema with the expected
    classification axes (validator-side check; verifies the registry shape
    even when no orchestrator runs)."""
    schema = EVENT_TYPE_SCHEMAS["asr_transcript_emitted"]
    assert schema.payload_kind == "transcript"
    assert schema.subject_class == "self"
    assert schema.sensitivity == "sensitive"
    assert schema.retention_policy_id == "transcript_audit_30d"
    for required in ("transcript_text", "signal_event_id", "asr_model_label"):
        assert required in schema.required_fields, (
            f"required_fields missing {required!r}: {schema.required_fields!r}"
        )
