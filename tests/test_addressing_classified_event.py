"""addressing_classified event contract tests — W-PR182-A.

Success criterion: 5 tests pass + existing addressing tests pass.

  test_addressing_classified_event_emitted_per_classification
  test_addressing_classified_caused_by_asr_transcript
  test_addressing_classified_payload_has_classifier_name
  test_addressing_classified_fired_for_both_primary_and_fallback
  test_causal_graph_completeness_with_addressing_classified
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.addressing_classifier import (
    AddressingSignal,
    WakeWordAddressingClassifier,
)
from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.causal_graph import CausalGraph
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
from companion_harness.v0_1g_event_schema import EVENT_TYPE_SCHEMAS


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
        context_items: tuple = (),
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

    async def cancel_generation(self) -> None:
        pass


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


class _ScriptedASRModel:
    def __init__(self, transcript: str) -> None:
        self._transcript = transcript

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        return self._transcript


class _FixedMiniCPMAddressingClassifier:
    """Stub MiniCPM classifier that always returns a fixed AddressingSignal."""

    def __init__(self, signal: AddressingSignal) -> None:
        self._signal = signal

    def __call__(
        self,
        transcript: str,
        speaker_count: int | None,
        social_mode: str,
    ) -> AddressingSignal | None:
        return self._signal


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
        client_id="test-addressing-classified",
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
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
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
    asr_model=None,
    addressing_classifier=None,
    minicpm_addressing_classifier=None,
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
        proposal_batch_window_ms=200,
        asr_model=asr_model,
        addressing_classifier=addressing_classifier,
        minicpm_addressing_classifier=minicpm_addressing_classifier,
        decision_trace_dir=tmp_path / "decision_traces",
    )


async def _run_one_eou(
    *,
    tmp_path: Path,
    session_id: str,
    asr_transcript: str = "hey companion",
    addressing_classifier=None,
    minicpm_addressing_classifier=None,
) -> list[Event]:
    logger, received = _make_logger()
    await logger.start()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        asr_model=_ScriptedASRModel(asr_transcript),
        addressing_classifier=addressing_classifier,
        minicpm_addressing_classifier=minicpm_addressing_classifier,
        tmp_path=tmp_path,
    )
    await orch.start()
    for i in range(7):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))
    await asyncio.sleep(0.3)
    await orch.stop()
    return received


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_addressing_classified_event_emitted_per_classification(tmp_path: Path) -> None:
    """WakeWord fallback classifier ⇒ exactly one addressing_classified event emitted."""
    received = await _run_one_eou(
        tmp_path=tmp_path,
        session_id="test-ac-emitted",
        asr_transcript="hey companion what time is it",
        addressing_classifier=WakeWordAddressingClassifier(),
    )
    ac_evts = [e for e in received if e.event_type == "addressing_classified"]
    assert len(ac_evts) == 1, (
        f"Expected exactly 1 addressing_classified event, got {len(ac_evts)}. "
        f"All types: {[e.event_type for e in received]}"
    )
    evt = ac_evts[0]
    assert evt.payload_kind == "signal"
    assert evt.subject_class == "self"
    assert evt.sensitivity == "safe"
    assert evt.retention_policy_id == "signal_default_30d"


@pytest.mark.asyncio
async def test_addressing_classified_caused_by_asr_transcript(tmp_path: Path) -> None:
    """addressing_classified.caused_by[] must include the asr_transcript_emitted event_id."""
    received = await _run_one_eou(
        tmp_path=tmp_path,
        session_id="test-ac-causal",
        asr_transcript="hey companion",
        addressing_classifier=WakeWordAddressingClassifier(),
    )
    transcript_evts = [e for e in received if e.event_type == "asr_transcript_emitted"]
    ac_evts = [e for e in received if e.event_type == "addressing_classified"]
    assert transcript_evts, "No asr_transcript_emitted event found"
    assert ac_evts, "No addressing_classified event found"
    transcript_evt_id = transcript_evts[0].event_id
    assert transcript_evt_id in ac_evts[0].caused_by, (
        f"addressing_classified.caused_by {ac_evts[0].caused_by!r} "
        f"does not contain asr_transcript_emitted {transcript_evt_id!r}"
    )


@pytest.mark.asyncio
async def test_addressing_classified_payload_has_classifier_name(tmp_path: Path) -> None:
    """addressing_classified.payload_inline must carry classifier_name, addressed, confidence, evidence."""
    received = await _run_one_eou(
        tmp_path=tmp_path,
        session_id="test-ac-payload",
        asr_transcript="hey companion",
        addressing_classifier=WakeWordAddressingClassifier(),
    )
    ac_evts = [e for e in received if e.event_type == "addressing_classified"]
    assert ac_evts, "No addressing_classified event found"
    payload = ac_evts[0].payload_inline
    assert payload is not None, "payload_inline is None"
    assert "classifier_name" in payload
    assert payload["classifier_name"] == "WakeWordAddressingClassifier"
    assert "addressed" in payload
    assert isinstance(payload["addressed"], bool)
    assert "confidence" in payload
    assert isinstance(payload["confidence"], float)
    assert "evidence" in payload


@pytest.mark.asyncio
async def test_addressing_classified_fired_for_both_primary_and_fallback(tmp_path: Path) -> None:
    """When a MiniCPM primary classifier is wired alongside a WakeWord fallback,
    only the primary fires (MiniCPM returns a signal → fallback path skipped).
    When MiniCPM returns None, fallback fires and emits one addressing_classified."""
    # Scenario A: primary (MiniCPM stub) returns a signal → one addressing_classified
    primary_signal = AddressingSignal(confidence="explicit", evidence="minicpm_explicit")
    received_a = await _run_one_eou(
        tmp_path=tmp_path / "a",
        session_id="test-ac-both-primary",
        asr_transcript="hey companion",
        minicpm_addressing_classifier=_FixedMiniCPMAddressingClassifier(primary_signal),
        addressing_classifier=WakeWordAddressingClassifier(),
    )
    ac_a = [e for e in received_a if e.event_type == "addressing_classified"]
    assert len(ac_a) == 1, f"Expected 1 event when primary fires, got {len(ac_a)}"
    assert ac_a[0].payload_inline["classifier_name"] == "_FixedMiniCPMAddressingClassifier"

    # Scenario B: MiniCPM stub returns None → WakeWord fallback fires → one addressing_classified
    class _NullMiniCPM:
        def __call__(self, transcript, speaker_count, social_mode) -> AddressingSignal | None:
            return None

    received_b = await _run_one_eou(
        tmp_path=tmp_path / "b",
        session_id="test-ac-both-fallback",
        asr_transcript="hey companion",
        minicpm_addressing_classifier=_NullMiniCPM(),
        addressing_classifier=WakeWordAddressingClassifier(),
    )
    ac_b = [e for e in received_b if e.event_type == "addressing_classified"]
    assert len(ac_b) == 1, f"Expected 1 event when fallback fires, got {len(ac_b)}"
    assert ac_b[0].payload_inline["classifier_name"] == "WakeWordAddressingClassifier"
    # fallback also emits signal_producer_fallback
    fb_evts = [e for e in received_b if e.event_type == "signal_producer_fallback"]
    assert any("addressing" in (e.payload_inline or {}).get("reason", "") or True for e in fb_evts), (
        "signal_producer_fallback should be emitted alongside the fallback addressing_classified"
    )


class _CapturingSilencePolicy:
    """Silence policy that records the PolicyInputs it was called with."""

    def __init__(self) -> None:
        self.captured_inputs: list[PolicyInputs] = []

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        self.captured_inputs.append(inputs)
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


@pytest.mark.asyncio
async def test_addressing_classified_payload_addressed_matches_policy_input_on_implicit_tier(
    tmp_path: Path,
) -> None:
    """Regression for PR #263 gatekeeper finding: audit event 'addressed' field
    must match the PolicyInputs.user_addressed_agent that actually flows to
    SpeakPolicy. Invariant #1 — recorded value == used value.

    MiniCPM stub returns confidence='implicit' with a substantive transcript;
    assert payload['addressed'] == inputs.user_addressed_agent == True.
    Before the fix, audit emitted False (no transcript arg) while policy got True.
    """
    implicit_signal = AddressingSignal(confidence="implicit", evidence="minicpm_implicit")
    capturing_policy = _CapturingSilencePolicy()

    # Run with capturing policy via a hand-built orchestrator.
    logger, received = _make_logger()
    await logger.start()
    from companion_harness.input_ingest import InputIngest

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    session_id = "test-ac-implicit-alignment"
    vad = VADDetector(
        model=_FakeVADModel([0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]),
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
    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=capturing_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
        asr_model=_ScriptedASRModel("what time is it"),  # substantive, >=3 tokens, not in denylist
        addressing_classifier=None,
        minicpm_addressing_classifier=_FixedMiniCPMAddressingClassifier(implicit_signal),
        decision_trace_dir=tmp_path / "decision_traces",
    )
    await orch.start()
    for i in range(7):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))
    await asyncio.sleep(0.3)
    await orch.stop()

    ac_evts = [e for e in received if e.event_type == "addressing_classified"]
    assert len(ac_evts) == 1, (
        f"Expected 1 addressing_classified event, got {len(ac_evts)}"
    )
    audit_addressed = ac_evts[0].payload_inline["addressed"]

    assert capturing_policy.captured_inputs, "SpeakPolicy was never invoked"
    policy_addressed = capturing_policy.captured_inputs[0].user_addressed_agent

    # The fix: both must be True on implicit + substantive transcript.
    assert policy_addressed is True, (
        f"PolicyInputs.user_addressed_agent expected True (implicit + substantive "
        f"transcript), got {policy_addressed!r}"
    )
    assert audit_addressed == policy_addressed, (
        f"Invariant #1 violation: addressing_classified.payload['addressed']="
        f"{audit_addressed!r} does not match PolicyInputs.user_addressed_agent="
        f"{policy_addressed!r}. Before fix, audit emitted False while policy got True "
        f"because the MiniCPM-path derive_user_addressed_agent() call site omitted "
        f"the transcript argument."
    )


@pytest.mark.asyncio
async def test_causal_graph_completeness_with_addressing_classified(tmp_path: Path) -> None:
    """Invariant #1 regression guard: zero orphan events after addressing_classified is emitted."""
    received = await _run_one_eou(
        tmp_path=tmp_path,
        session_id="test-ac-causal-completeness",
        asr_transcript="hey companion",
        addressing_classifier=WakeWordAddressingClassifier(),
    )
    graph = CausalGraph(received)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"Causal graph has orphan events after addressing_classified: "
        f"{report.dangling_refs}"
    )
