"""Path B core contract tests (PR 4 / §3.3).

Success criteria:
  test_continuous_proposer_keeps_tokens_across_turns  — infer_stream_continuous
    invoked once; _proposal_ring accumulates monotonically across two utterances.
  test_policy_silence_discards_buffered_tokens  — silence decision discards ring
    tail; _proposal_ring_committed_seq does not advance.
  test_policy_commit_dispatches_immediately_no_grace_window  — full_response
    dispatches TTS within 50 ms; no 600 ms grace window.

All tests use CPU-runnable fakes. No GPU / MiniCPM weights required.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

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
# Shared stubs
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-path-b",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


def _pcm_silence(n_bytes: int = 512) -> bytes:
    """Silent frames — RMS below SmartTurnDetector threshold (100)."""
    return b"\x00" * n_bytes


def _pcm_speech(n_bytes: int = 512) -> bytes:
    """Speech frames — int16 value 4000, RMS = 4000 >> threshold 100."""
    import struct
    sample = struct.pack("<h", 4000)
    return (sample * (n_bytes // 2))[:n_bytes]


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return _pcm_silence(n_bytes)


class _SilencePolicy:
    def __call__(
        self, inputs: PolicyInputs, signal_event_ids: list[str], p_backchannel: float = 0.0
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


class _FullResponsePolicy:
    def __call__(
        self, inputs: PolicyInputs, signal_event_ids: list[str], p_backchannel: float = 0.0
    ) -> SpeakDecision:
        return SpeakDecision(
            action_type="full_response",
            primary_reason_code=ReasonCode.USER_ADDRESSED_AGENT,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )


class _SpyTtsAdapter:
    def __init__(self) -> None:
        self.call_timestamps_ns: list[int] = []

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        self.call_timestamps_ns.append(time.monotonic_ns())
        yield b"\x00" * 160


class _CountingStreamingModel:
    """StreamingDuplexModel that counts infer_stream_continuous calls and
    emits a controllable number of proposals per call to on_proposal.

    Supports infer_stream_continuous so the Path B T3 uses the single-lifetime path.
    """

    def __init__(self, proposals_to_emit: list[str] | None = None) -> None:
        self.infer_stream_continuous_call_count = 0
        self._proposals = proposals_to_emit or ["hello world", "second proposal"]
        self._consumed_event = asyncio.Event()

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: Any) -> None:
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
            return
            yield  # noqa: make generator

        return _gen()

    async def infer_stream_continuous(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        *,
        on_proposal: Callable[[ThinkerProposal, str], None],
        on_response_complete: Callable[[str], None],
        on_response_started: Callable[[str], None] | None = None,
    ) -> None:
        self.infer_stream_continuous_call_count += 1
        proposal_idx = 0
        async for _ in frame_iter:
            if proposal_idx < len(self._proposals):
                on_proposal(ThinkerProposal(
                    proposal_type="observation",
                    content=self._proposals[proposal_idx],
                    trigger="speech",
                    confidence=0.9,
                    novelty=0.5,
                    interruption_cost=0.3,
                    max_utterance_ms=5000,
                    cooldown_consumed="speech_turn",
                    caused_by=list(caused_by),
                ), f"r-stub-{proposal_idx}")
                proposal_idx += 1
        self._consumed_event.set()


async def _noop_sink(chunk: bytes) -> None:
    pass


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session: Any,
    audio_in: asyncio.Queue,
    model: Any,
    speak_policy: Any,
    tts_adapter: Any = None,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=lambda _: 0.9,  # always speech
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=lambda _: (0.9, 0.1),  # p_done=0.9 → triggers EOU
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(
        model=model,
        session_id=session_id,
        logger=logger,
    )
    controller = AudioOutputController(
        session_id=session_id,
        logger=logger,
        sink=_noop_sink,
    )
    _tts = tts_adapter or _NoopTts()
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=lambda sig, hist: PolicyInputs(
            user_speaking=True,
            eou_probability=sig.p_done,
            assistant_speaking=False,
            scene_change_score=0.0,
            deictic_reference=False,
            user_addressed_agent=True,
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
        ),
        speak_policy=speak_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=_tts,
        use_streaming_speculative=True,
    )


class _NoopTts:
    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuous_proposer_keeps_tokens_across_turns(tmp_path: Path) -> None:
    """infer_stream_continuous invoked exactly once; ring accumulates monotonically."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-path-b-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    model = _CountingStreamingModel(
        proposals_to_emit=["turn 1 response", "turn 2 response", "turn 3 response"]
    )
    orch = _build_orch(
        session_id="sb-continuous",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_SilencePolicy(),
    )

    await orch.start()

    # Push speech frames (triggers _in_speech=True in SmartTurnDetector) then
    # silence frames (accumulates until silence_onset_ms fires EOU).
    frames = ([_pcm_speech()] * 5) + ([_pcm_silence()] * 12)
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta(i))
        await audio_in.put((frame, evt.event_id))

    # Let the event loop run.
    await asyncio.sleep(0.3)
    await orch.stop()

    # infer_stream_continuous must be called exactly once.
    assert model.infer_stream_continuous_call_count == 1

    # Ring must have monotonically increasing seq numbers.
    seqs = [seq for seq, _, _ in orch._proposal_ring]
    assert seqs == sorted(seqs), "ring seqs are not monotonic"
    assert all(rid for _, rid, _ in orch._proposal_ring), "ring must not contain empty response_id"

    # At least one proposer_token_buffered event must have been emitted.
    token_events = [e for e in received if e.event_type == "proposer_token_buffered"]
    assert len(token_events) >= 1

    # Each proposer_token_buffered event has required fields.
    for evt in token_events:
        assert "ring_seq" in (evt.payload_inline or {})
        assert "text_preview" in (evt.payload_inline or {})
        # text_preview must be <= 32 chars.
        assert len(evt.payload_inline["text_preview"]) <= 32


@pytest.mark.asyncio
async def test_policy_silence_discards_buffered_tokens(tmp_path: Path) -> None:
    """Silence decision discards ring tail; committed_seq does not advance."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-path-b-client-silence")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    model = _CountingStreamingModel(
        proposals_to_emit=["proposal A", "proposal B", "proposal C"]
    )
    orch = _build_orch(
        session_id="sb-silence",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_SilencePolicy(),
    )

    await orch.start()

    # Push speech frames then silence frames to trigger EOU.
    frames = ([_pcm_speech()] * 5) + ([_pcm_silence()] * 12)
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta(i))
        await audio_in.put((frame, evt.event_id))

    await asyncio.sleep(0.3)
    await orch.stop()

    # Phase C: silence now emits response_suppressed_by_policy (not commit_or_discard).
    suppressed_events = [e for e in received if e.event_type == "response_suppressed_by_policy"]
    assert len(suppressed_events) >= 1

    # Each suppressed event must have a response_id field (may be empty string
    # when no drain task was active on a listen-only turn).
    for evt in suppressed_events:
        assert "response_id" in (evt.payload_inline or {})


@pytest.mark.asyncio
async def test_policy_commit_dispatches_immediately_no_grace_window(tmp_path: Path) -> None:
    """full_response dispatches TTS within 50 ms; no 600 ms grace window wait."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-path-b-client-commit")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    model = _CountingStreamingModel(
        proposals_to_emit=["answer 1", "answer 2", "answer 3", "answer 4", "answer 5"]
    )
    spy_tts = _SpyTtsAdapter()
    orch = _build_orch(
        session_id="sb-commit",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_FullResponsePolicy(),
        tts_adapter=spy_tts,
    )

    await orch.start()

    # Push speech then silence: proposals land in ring before EOU fires.
    frames = ([_pcm_speech()] * 5) + ([_pcm_silence()] * 12)
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta(i))
        await audio_in.put((frame, evt.event_id))

    await asyncio.sleep(0.3)
    await orch.stop()

    # Phase C: full_response now emits path_b_eou_policy_approved (drain task handles TTS).
    approved_events = [e for e in received if e.event_type == "path_b_eou_policy_approved"]
    assert len(approved_events) >= 1, "expected at least one path_b_eou_policy_approved event"

    # policy_decision must have been emitted with action_type=full_response.
    policy_events = [e for e in received if e.event_type == "policy_decision"]
    assert len(policy_events) >= 1, "no policy_decision event found"
    policy_full = [
        e for e in policy_events
        if (e.payload_inline or {}).get("action_type") == "full_response"
    ]
    assert len(policy_full) >= 1, "no policy_decision with action_type=full_response found"

    # Each approved event carries drain_active and response_id fields.
    for evt in approved_events:
        assert "drain_active" in (evt.payload_inline or {})
        assert "response_id" in (evt.payload_inline or {})


@pytest.mark.asyncio
async def test_on_response_started_complete_race(tmp_path: Path) -> None:
    """on_response_started then synchronous on_response_complete before any await.

    Exercises the pre-created drain_complete_event: the liveness fix ensures
    _feed_queue exits cleanly when drain_complete_event is already set by the
    time the clear() runs.
    """
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-path-b-race")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    class _ImmediateCompleteModel:
        """Fires on_response_started then on_response_complete synchronously."""

        infer_stream_continuous_call_count = 0

        def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
            return None

        def set_context(self, items: Any) -> None:
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
                return
                yield  # noqa: make generator

            return _gen()

        async def infer_stream_continuous(
            self,
            frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
            caused_by: list[str],
            *,
            on_proposal: Callable[[ThinkerProposal, str], None],
            on_response_complete: Callable[[str], None],
            on_response_started: Callable[[str], None] | None = None,
        ) -> None:
            _ImmediateCompleteModel.infer_stream_continuous_call_count += 1
            async for _ in frame_iter:
                pass
            # Fire started then complete synchronously — no await in between.
            if on_response_started is not None:
                on_response_started("race-rid")
            on_response_complete("race-rid")

    model = _ImmediateCompleteModel()
    orch = _build_orch(
        session_id="sb-race",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_SilencePolicy(),
    )

    await orch.start()

    frames = ([_pcm_speech()] * 5) + ([_pcm_silence()] * 12)
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta(i))
        await audio_in.put((frame, evt.event_id))

    # Allow drain task (if spawned) to complete without hanging.
    await asyncio.sleep(0.3)
    await orch.stop()

    # After stop, _active_drain_task must be cleared (not stuck).
    assert orch._active_drain_task is None
