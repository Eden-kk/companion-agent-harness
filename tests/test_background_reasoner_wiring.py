"""Orchestrator smart-path wiring test (v0.1f Task 9).

Success criterion:
  pytest tests/test_background_reasoner_wiring.py::test_smart_path_context_injection

FakeBackgroundReasoner + reasoner-yielded events flow to EventLogger;
set_context() called with summarized results.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, AsyncGenerator
from datetime import datetime, timezone

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.background_reasoner import FakeBackgroundReasoner
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, MemoryItem, PolicyInputs, SpeakDecision, ThinkerProposal, TurnSignal
from companion_harness.tool_router import ToolDispatchRequest
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=1024), received


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-client",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


class _FakeVADModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _CapturingFakeStreamingModel:
    """Records set_context() calls for assertion."""

    def __init__(self) -> None:
        self.set_context_calls: list[list[MemoryItem]] = []

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        self.set_context_calls.append(list(items))

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
            yield  # type: ignore[misc]

        return _gen()


class _NoopTtsAdapter:
    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        return
        yield  # type: ignore[misc]


async def _noop_sink(chunk: bytes) -> None:
    pass


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
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


def _silence_policy(
    inputs: PolicyInputs,
    signal_event_ids: list[str],
    p_backchannel: float = 0.0,
    **kwargs,
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
        response_content_source="no_synthesis",
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_smart_path_context_injection(tmp_path) -> None:
    """FakeBackgroundReasoner events reach EventLogger; set_context() is called."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)

    vad = VADDetector(
        model=_FakeVADModel(),
        session_id="s-smart",
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id="s-smart",
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=_FakeBackchannelModel(),
        session_id="s-smart",
        logger=logger,
    )
    capturing_model = _CapturingFakeStreamingModel()
    fg = ForegroundModel(
        model=capturing_model,
        session_id="s-smart",
        logger=logger,
    )
    controller = AudioOutputController(
        session_id="s-smart",
        logger=logger,
        sink=_noop_sink,
    )
    bg_reasoner = FakeBackgroundReasoner(session_id="s-smart")

    orch = StreamingRealtimeOrchestrator(
        session_id="s-smart",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=_silence_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=_NoopTtsAdapter(),
        proposal_batch_window_ms=50,
        background_reasoner=bg_reasoner,
    )

    await orch.start()
    try:
        # Enqueue a smart-path dispatch request directly.
        request = ToolDispatchRequest(
            tool_name="search_web",
            arguments={"q": "test"},
            caused_by=["upstream-evt-1"],
            routing_hint="smart",
        )
        orch.dispatch_smart(request)

        # Allow the T5 smart-path task to process the request.
        await asyncio.sleep(0.05)
    finally:
        await orch.stop()

    # Events from FakeBackgroundReasoner must have been forwarded to the logger.
    event_types = [e.event_type for e in received]
    assert "background_reasoning_started" in event_types, f"got: {event_types}"
    assert "background_reasoning_completed" in event_types, f"got: {event_types}"

    # set_context() must have been called with the summarized MemoryItems.
    assert len(capturing_model.set_context_calls) >= 1, "set_context() was not called"
    items = capturing_model.set_context_calls[-1]
    assert len(items) == 1
    assert items[0].store == "episodic"
    assert items[0].content["tool_name"] == "search_web"
