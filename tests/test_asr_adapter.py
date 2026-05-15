"""ASR adapter contract tests — v0.1f wiring of whisper-tiny.en into the orchestrator.

Success criterion (verbatim):
  pytest -k asr_adapter passes non-vacuously.
  - test_asr_adapter_satisfies_protocol: runtime_checkable Protocol is satisfied
    by the fake.
  - test_orchestrator_calls_asr_on_eou: when an asr_model is injected, the
    orchestrator calls it exactly once per EOU and PolicyInputs.user_transcript
    carries the scripted string.
  - test_orchestrator_no_asr_means_empty_transcript: asr_model=None →
    PolicyInputs.user_transcript stays "".
  - test_asr_buffer_cleared_between_turns: turn 1 → "hello"; turn 2 → "world";
    the second invocation sees the new turn's audio only.

NO faster_whisper import. Tests run on machines without GPU.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.asr_adapter import ASRModel
from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import Event, PolicyInputs, SpeakDecision, ThinkerProposal, TurnSignal
from companion_harness.reason_codes import ReasonCode
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeASRModel:
    """ASRModel fake: returns scripted transcripts in order, records audio bytes."""

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)
        self._call_count = 0
        self.audio_seen: list[bytes] = []

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        self.audio_seen.append(audio_chunks)
        if self._call_count >= len(self._scripted):
            self._call_count += 1
            return ""
        out = self._scripted[self._call_count]
        self._call_count += 1
        return out

    @property
    def call_count(self) -> int:
        return self._call_count


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
    """Yields one proposal per batch (so the path through T3 terminates)."""

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


class _RecordingPolicy:
    """Records inputs.user_transcript on every decide() call. Returns silence so no TTS path executes."""

    def __init__(self) -> None:
        self.transcripts_seen: list[str] = []

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        self.transcripts_seen.append(inputs.user_transcript)
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
        client_id="test-asr",
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
    speak_policy=None,
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
        speak_policy=speak_policy,
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_asr_adapter_satisfies_protocol() -> None:
    """The fake satisfies the runtime_checkable ASRModel Protocol."""
    fake = _FakeASRModel(["hello"])
    assert isinstance(fake, ASRModel), "Fake ASR does not satisfy ASRModel Protocol"


@pytest.mark.asyncio
async def test_orchestrator_calls_asr_on_eou(tmp_path: Path) -> None:
    """When asr_model is injected, the orchestrator calls it on EOU and
    PolicyInputs.user_transcript carries the scripted string."""
    logger, _received = _make_logger()
    await logger.start()
    try:
        ingest = InputIngest(logger=logger, blob_dir=tmp_path)
        session = ingest.open_session("test-client")
        audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
        fake_asr = _FakeASRModel(["hello world"])
        policy = _RecordingPolicy()

        orch = _build_orch(
            session_id="test-asr-eou",
            logger=logger,
            ingest_session=session,
            audio_in=audio_in,
            vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
            asr_model=fake_asr,
            speak_policy=policy,
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

    assert fake_asr.call_count >= 1, "ASR was not called on EOU"
    # The recording policy is invoked on each EOU; the first EOU must have seen
    # the scripted transcript.
    assert "hello world" in policy.transcripts_seen, (
        f"PolicyInputs.user_transcript missing scripted ASR output. "
        f"Seen: {policy.transcripts_seen!r}"
    )


@pytest.mark.asyncio
async def test_orchestrator_no_asr_means_empty_transcript(tmp_path: Path) -> None:
    """asr_model=None → PolicyInputs.user_transcript stays "" (backward compatible)."""
    logger, _received = _make_logger()
    await logger.start()
    try:
        ingest = InputIngest(logger=logger, blob_dir=tmp_path)
        session = ingest.open_session("test-client")
        audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
        policy = _RecordingPolicy()

        orch = _build_orch(
            session_id="test-asr-none",
            logger=logger,
            ingest_session=session,
            audio_in=audio_in,
            vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
            asr_model=None,
            speak_policy=policy,
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

    assert policy.transcripts_seen, "Policy was not invoked — test setup is broken"
    for transcript in policy.transcripts_seen:
        assert transcript == "", (
            f"With asr_model=None, transcript must stay empty. Saw: {transcript!r}"
        )


@pytest.mark.asyncio
async def test_asr_buffer_cleared_between_turns(tmp_path: Path) -> None:
    """Turn 1 audio must not leak into turn 2's ASR invocation."""
    logger, _received = _make_logger()
    await logger.start()
    try:
        ingest = InputIngest(logger=logger, blob_dir=tmp_path)
        session = ingest.open_session("test-client")
        audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
        fake_asr = _FakeASRModel(["hello", "world"])
        policy = _RecordingPolicy()

        # Two speech→silence cycles to drive two EOUs.
        vad_script = [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1,
                      0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
        orch = _build_orch(
            session_id="test-asr-cleared",
            logger=logger,
            ingest_session=session,
            audio_in=audio_in,
            vad_probs=vad_script,
            asr_model=fake_asr,
            speak_policy=policy,
            tmp_path=tmp_path,
        )

        await orch.start()
        # Turn 1
        await _push_frames(audio_in, ingest, session, 7, start_i=0)
        await asyncio.sleep(0.25)
        bytes_after_turn1 = sum(len(b) for b in fake_asr.audio_seen)
        # Turn 2
        await _push_frames(audio_in, ingest, session, 7, start_i=7)
        await asyncio.sleep(0.25)
        await orch.stop()
    finally:
        try:
            await logger.stop()
        except Exception:
            pass

    assert fake_asr.call_count >= 2, (
        f"Expected at least 2 ASR calls (one per EOU), got {fake_asr.call_count}"
    )

    # Each invocation's audio bytes must be bounded by the per-turn frame count.
    # 7 frames * 512 bytes = 3584 bytes. If the buffer leaked, turn 2 would see
    # the cumulative ~7168 bytes. Bound to 7 frames + slack for any ingest jitter.
    per_turn_max = 7 * 512 + 512  # one-frame slack
    for i, audio in enumerate(fake_asr.audio_seen):
        assert len(audio) <= per_turn_max, (
            f"ASR call {i} saw {len(audio)} bytes — buffer leaked across turns "
            f"(expected ≤ {per_turn_max})"
        )

    # Sanity: turn 1 saw a non-trivial chunk and turn 2 also did.
    assert bytes_after_turn1 > 0
    assert fake_asr.audio_seen[1] != b"", "Turn 2 ASR call received empty audio"

    # Both scripted transcripts must have flowed through the policy.
    assert "hello" in policy.transcripts_seen, (
        f"Turn 1 transcript missing. Seen: {policy.transcripts_seen!r}"
    )
    assert "world" in policy.transcripts_seen, (
        f"Turn 2 transcript missing. Seen: {policy.transcripts_seen!r}"
    )
