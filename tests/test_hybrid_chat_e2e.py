"""Integration tests for Option C Stage 3: hybrid chat-stream e2e.

Two scenarios:
1. natural_end: 8 chat deltas emitted → hybrid_mode_returned_to_duplex trigger="natural_end"
2. barge_in_mid_stream: VAD onset injected mid-stream →
   hybrid_mode_returned_to_duplex trigger="barge_in"
   timing assertion uses event timestamps (not wall-clock) for CI determinism.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, PolicyInputs, SpeakDecision, ThinkerProposal
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Fake adapters
# ---------------------------------------------------------------------------


class _FakeHybridModel:
    """Stub model: infer_stream no-ops; chat_stream_turn emits scripted deltas."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = deltas

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
            yield  # noqa: unreachable

        return _gen()

    async def chat_stream_turn(
        self,
        audio: np.ndarray,
        *,
        context_items: tuple = (),
        caused_by: list[str],
    ) -> AsyncGenerator[ThinkerProposal, None]:
        deltas = list(self._deltas)

        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            for text in deltas:
                await asyncio.sleep(0.01)  # simulate inference latency per delta
                yield ThinkerProposal(
                    proposal_type="observation",
                    content=text,
                    trigger="eou",
                    confidence=0.9,
                    novelty=0.5,
                    interruption_cost=0.1,
                    max_utterance_ms=5000,
                    cooldown_consumed="full_response",
                    caused_by=caused_by,
                )

        return _gen()

    def request_chat_stop(self) -> None:
        pass

    def reset_streaming_session(self, *, caused_by: list[str]) -> None:
        pass


class _SlowStreamingTtsAdapter:
    """synthesize_streaming yields one chunk per text chunk with a sleep."""

    def __init__(self, chunk_delay_ms: float = 30) -> None:
        self._delay_s = chunk_delay_ms / 1000.0

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b"\x00" * 320

    async def synthesize_streaming(
        self,
        text_chunks: AsyncIterator[str],
        prosody_tags: list[str],
    ) -> AsyncIterator[bytes]:
        async for _chunk in text_chunks:
            await asyncio.sleep(self._delay_s)
            yield b"\x00" * 320


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


def _pcm_chunk(n_samples: int = 160) -> bytes:
    """PCM16 silence (1 byte per sample * 2 bytes → n_samples * 2)."""
    return b"\x00" * (n_samples * 2)


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-hybrid",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


def _build_policy_inputs(signal, signal_history) -> PolicyInputs:
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


def _long_response_policy(
    inputs: PolicyInputs,
    signal_event_ids: list[str],
    p_backchannel: float = 0.0,
    **kwargs: Any,
) -> SpeakDecision:
    """Return LONG_RESPONSE_GATED on high EOU probability; silence otherwise."""
    if inputs.eou_probability >= 0.8:
        return SpeakDecision(
            action_type="full_response",
            primary_reason_code=ReasonCode.LONG_RESPONSE_GATED,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket="full_response",
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )
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
    ingest_session: Any,
    audio_in: asyncio.Queue,
    deltas: list[str],
    tts_adapter: Any | None = None,
    hard_cancel_after_ms: int = 30,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=lambda _: (0.1, 0.9),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
    )
    model = _FakeHybridModel(deltas=deltas)
    fg = ForegroundModel(model=model, session_id=session_id, logger=logger)
    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink)
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=_long_response_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=tts_adapter or _SlowStreamingTtsAdapter(chunk_delay_ms=15),
        proposal_batch_window_ms=50,
        hard_cancel_after_ms=hard_cancel_after_ms,
        use_hybrid=True,
    )


async def _push_audio_with_high_eou(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session: Any,
    *,
    count: int,
    start_i: int = 0,
    p_speech: float = 0.9,
) -> None:
    """Push `count` frames. The VAD model is a stub (returns 0.0), so these
    accumulate audio for hybrid but do not trigger VAD onset."""
    for i in range(count):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(start_i + i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


# ---------------------------------------------------------------------------
# §2.6 Test 1: natural_end
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_chat_stream_natural_end(tmp_path: Path) -> None:
    """Hybrid chat-stream runs to completion; event order and trigger verified."""
    deltas = [f"word{i}" for i in range(8)]  # 8 deltas, 5-char each
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-hybrid-natural-end"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        deltas=deltas,
    )
    await orch.start()

    # Push 3 seconds of audio (≥ _MIN_HYBRID_CHAT_AUDIO_SAMPLES * 2 bytes = 32000 bytes).
    # Each _pcm_chunk() = 320 bytes; we need ≥ 100 chunks → push 110.
    for i in range(110):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))

    # Directly trigger an EOU decision by enqueuing a TurnSignal via _t2_inbox.
    from companion_harness.schemas import TurnSignal

    turn_signal = TurnSignal(
        detector="smart_turn",
        p_done=0.9,
        p_continue=0.1,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=["test-sig-natural-end-0001"],
    )
    await orch._t2_inbox.put((turn_signal, "test-sig-natural-end-0001"))

    # Wait for natural end: 8 deltas × 10ms inference + 8 × 15ms TTS = ~200ms.
    await asyncio.sleep(1.5)
    await orch.stop()

    types = [e.event_type for e in received]
    types_set = set(types)

    # Assert required events present
    assert "chat_stream_turn_started" in types_set, (
        f"Missing chat_stream_turn_started. Got types: {sorted(types_set)}"
    )
    assert "chat_stream_text_delta" in types_set, (
        f"Missing chat_stream_text_delta. Got types: {sorted(types_set)}"
    )
    assert "hybrid_mode_returned_to_duplex" in types_set, (
        f"Missing hybrid_mode_returned_to_duplex. Got types: {sorted(types_set)}"
    )

    # At least 5 chat_stream_text_delta events
    delta_count = sum(1 for e in received if e.event_type == "chat_stream_text_delta")
    assert delta_count >= 5, f"Expected ≥5 chat_stream_text_delta, got {delta_count}"

    # hybrid_mode_returned_to_duplex has trigger="natural_end"
    returned_evts = [e for e in received if e.event_type == "hybrid_mode_returned_to_duplex"]
    assert returned_evts, "No hybrid_mode_returned_to_duplex event"
    returned = returned_evts[-1]
    assert returned.payload_inline is not None
    assert returned.payload_inline.get("trigger") == "natural_end", (
        f"Expected trigger='natural_end', got {returned.payload_inline!r}"
    )

    # Event order: chat_stream_turn_started must precede all chat_stream_text_delta events,
    # which must precede hybrid_mode_returned_to_duplex.
    started_idx = next(i for i, e in enumerate(received) if e.event_type == "chat_stream_turn_started")
    delta_indices = [i for i, e in enumerate(received) if e.event_type == "chat_stream_text_delta"]
    returned_idx = next(i for i, e in enumerate(received) if e.event_type == "hybrid_mode_returned_to_duplex")
    assert all(started_idx < d for d in delta_indices), "chat_stream_text_delta before chat_stream_turn_started"
    assert all(d < returned_idx for d in delta_indices), "hybrid_mode_returned_to_duplex before deltas"

    # chat_stream_text_delta events chain to chat_stream_turn_started event_id
    started_evt_id = next(e.event_id for e in received if e.event_type == "chat_stream_turn_started")
    for d_evt in [e for e in received if e.event_type == "chat_stream_text_delta"]:
        assert started_evt_id in d_evt.caused_by, (
            f"chat_stream_text_delta.caused_by={d_evt.caused_by!r} does not reference "
            f"chat_stream_turn_started {started_evt_id!r}"
        )


# ---------------------------------------------------------------------------
# §2.6 Test 2: barge_in_mid_stream
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_chat_stream_barge_in_mid_stream(tmp_path: Path) -> None:
    """Barge-in during chat-stream sets trigger='barge_in' in returned_to_duplex.

    Timing assertion uses event-timestamp delta (NOT wall-clock) to avoid CI flakiness.
    onset event timestamp_mono_ms → hybrid_chat_stop_requested event timestamp_mono_ms.
    """
    deltas = [f"word{i:02d}" for i in range(8)]  # 8 deltas
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-hybrid-barge-in"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        deltas=deltas,
        tts_adapter=_SlowStreamingTtsAdapter(chunk_delay_ms=50),
        hard_cancel_after_ms=20,
    )
    await orch.start()

    # Push 110 frames to fill the audio buffer (≥ 1s of audio needed for snapshot guard).
    for i in range(110):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))

    # Trigger EOU via TurnSignal injection.
    from companion_harness.schemas import TurnSignal

    turn_signal = TurnSignal(
        detector="smart_turn",
        p_done=0.9,
        p_continue=0.1,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=["test-sig-barge-in-0001"],
    )
    await orch._t2_inbox.put((turn_signal, "test-sig-barge-in-0001"))

    # Wait briefly for CHAT_STREAMING state to be entered.
    for _ in range(50):
        await asyncio.sleep(0.01)
        if orch._hybrid_state == "CHAT_STREAMING":
            break

    # Inject a VAD onset frame directly into T2 inbox with high p_speech.
    # This replicates what T1 sends during actual speech; p_speech > 0.5 (threshold).
    from companion_harness.realtime_orchestrator import _VadOnsetFrame

    onset_frame = _VadOnsetFrame(p_speech=0.9, frame_event_id="test-onset-barge-in-0001")
    await orch._t2_inbox.put(onset_frame)

    # Wait for barge-in to complete + state to return to AMBIENT_DUPLEX.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if orch._hybrid_state == "AMBIENT_DUPLEX":
            break

    await orch.stop()

    types_set = {e.event_type for e in received}

    assert "hybrid_chat_stop_requested" in types_set, (
        f"Missing hybrid_chat_stop_requested. Got: {sorted(types_set)}"
    )
    assert "hybrid_mode_returned_to_duplex" in types_set, (
        f"Missing hybrid_mode_returned_to_duplex. Got: {sorted(types_set)}"
    )

    returned_evts = [e for e in received if e.event_type == "hybrid_mode_returned_to_duplex"]
    assert returned_evts, "No hybrid_mode_returned_to_duplex"
    returned = returned_evts[-1]
    assert returned.payload_inline is not None
    assert returned.payload_inline.get("trigger") == "barge_in", (
        f"Expected trigger='barge_in', got {returned.payload_inline!r}"
    )

    # Event-timestamp delta: onset → hybrid_chat_stop_requested ≤ 200ms.
    onset_evts = [e for e in received if e.event_type == "vad_user_speech_onset"]
    stop_req_evts = [e for e in received if e.event_type == "hybrid_chat_stop_requested"]
    if onset_evts and stop_req_evts:
        onset_ms = onset_evts[-1].timestamp_mono_ms
        stop_ms = stop_req_evts[-1].timestamp_mono_ms
        if onset_ms is not None and stop_ms is not None:
            delta_ms = stop_ms - onset_ms
            assert delta_ms <= 200, (
                f"onset→hybrid_chat_stop_requested delta {delta_ms}ms exceeds 200ms bound"
            )


# ---------------------------------------------------------------------------
# Race: barge-in fires during EOU_PENDING_SWITCH before CHAT_STREAMING begins
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_chat_stream_barge_in_before_drain_completes(tmp_path: Path) -> None:
    """Barge-in during EOU_PENDING_SWITCH (play_task still None) must not AssertionError.

    The race: _fire_barge_in early-return path transitions state to AMBIENT_DUPLEX;
    T4 finally block must not then try AMBIENT_DUPLEX → RESETTING_TO_DUPLEX.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-hybrid-race"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        deltas=["w0", "w1", "w2"],
        tts_adapter=_SlowStreamingTtsAdapter(chunk_delay_ms=100),
        hard_cancel_after_ms=20,
    )
    await orch.start()

    for i in range(110):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))

    from companion_harness.realtime_orchestrator import _VadOnsetFrame
    from companion_harness.schemas import TurnSignal

    turn_signal = TurnSignal(
        detector="smart_turn",
        p_done=0.9,
        p_continue=0.1,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=["test-race-eou-0001"],
    )
    # Inject EOU and barge-in back-to-back with a single yield between them so
    # the event loop processes EOU (→ EOU_PENDING_SWITCH) before the onset fires,
    # but _fire_barge_in runs while play_task is still None.
    await orch._t2_inbox.put((turn_signal, "test-race-eou-0001"))
    await asyncio.sleep(0)
    onset_frame = _VadOnsetFrame(p_speech=0.9, frame_event_id="test-race-onset-0001")
    await orch._t2_inbox.put(onset_frame)

    # Wait for state to settle back to AMBIENT_DUPLEX.
    for _ in range(100):
        await asyncio.sleep(0.02)
        if orch._hybrid_state == "AMBIENT_DUPLEX":
            break

    await orch.stop()

    assert orch._hybrid_state == "AMBIENT_DUPLEX", (
        f"Expected AMBIENT_DUPLEX after race, got {orch._hybrid_state}"
    )
