"""v0.1f Task 7: barge-in cancels in-flight tool dispatch (300ms p50 gate).

Success criterion:
  pytest tests/test_tool_router_cancel.py::test_barge_in_cancels_in_flight_tool

Covers:
  - _fire_barge_in() calls tool_router.cancel(tool_call_id) when a tool
    dispatch is in-flight.
  - dispatch() emits tool_call_cancelled (NOT tool_call_completed).
  - tool_call_cancelled event is forwarded to the EventLogger.

All models are fakes — no torch, no SDK imports (adapter-first).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.fake_tool_router import FakeToolRouter
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    ThinkerProposal,
    TurnSignal,
)
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

    return EventLogger(sink, maxsize=4096), received


async def _noop_sink(chunk: bytes) -> None:
    pass


# ---------------------------------------------------------------------------
# Fake adapters (minimal — only what the orchestrator constructor needs)
# ---------------------------------------------------------------------------


class _FakeVADModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


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
    ):
        async def _gen():
            async for _ in frame_iter:
                pass
            return
            yield

        return _gen()


class _SilentTtsAdapter:
    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b"\x00" * 160


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
        eou_probability=0.5,
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
    tool_router: FakeToolRouter,
    tmp_path: Path,
) -> StreamingRealtimeOrchestrator:
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
        tts_adapter=_SilentTtsAdapter(),
        tool_router=tool_router,
        decision_trace_dir=tmp_path / "traces",
    )


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_barge_in_cancels_in_flight_tool(tmp_path: Path):
    """_fire_barge_in calls tool_router.cancel() when a tool dispatch is in-flight.

    Directly exercises the barge-in → cancel path:
    1. Start a tool dispatch as a background task.
    2. Set orchestrator._inflight_tool_call_id (as T4 does after yielding).
    3. Plant a non-done play_task on audio_output so _fire_barge_in proceeds
       past its early-exit guard.
    4. Call _fire_barge_in; verify cancel() was invoked.
    5. Dispatch must return tool_call_cancelled, not tool_call_completed.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-cancel-tool"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    # FakeToolRouter with multiple progress stages so dispatch yields several
    # times, giving cancel() a window to fire between stages.
    router = FakeToolRouter(
        session_id=session_id,
        progress_stages=["started", "scanning", "aggregating"],
    )

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        tool_router=router,
        tmp_path=tmp_path,
    )
    await orch.start()

    # Start dispatch as a background task (mirrors what T4 does).
    request = ToolDispatchRequest(
        tool_name="search",
        arguments={"q": "test"},
        caused_by=[session.session_open_id],
    )
    dispatch_task = asyncio.get_running_loop().create_task(router.dispatch(request))

    # Yield once — dispatch registers its cancel flag before its first await.
    await asyncio.sleep(0)

    # Capture the in-flight tool_call_id (as T4 does via _cancel_flags inspection).
    assert router._cancel_flags, "Router must have a cancel flag registered after first yield"
    tool_call_id = next(iter(router._cancel_flags))
    orch._inflight_tool_call_id = tool_call_id

    # Plant a never-finishing play_task on audio_output so _fire_barge_in
    # proceeds past its "play_task is None or done" early-exit guard.
    async def _forever() -> None:
        await asyncio.sleep(9999)

    fake_play_task = asyncio.get_running_loop().create_task(_forever())
    orch._audio_output.set_generation_task(fake_play_task)
    orch._audio_output._playing = True  # reflect is_playing = True

    # Fire barge-in: should call tool_router.cancel(tool_call_id).
    onset_evt_id = orch._emit("vad_user_speech_onset", [session.session_open_id], "signal").event_id
    await orch._fire_barge_in(onset_evt_id)

    # _fire_barge_in cancels the fake play task; dispatch_task still running.
    result = await dispatch_task
    orch._inflight_tool_call_id = None

    # Forward events to logger (as T4 would).
    for evt in result.events:
        logger.log(evt)

    await orch.stop()

    event_types = [e.event_type for e in result.events]
    assert "tool_call_cancelled" in event_types, (
        f"Expected tool_call_cancelled in dispatch events; got: {event_types}"
    )
    assert "tool_call_completed" not in event_types, (
        f"tool_call_completed must not appear when cancelled; got: {event_types}"
    )
    assert result.final_status == "cancelled"

    # tool_call_cancelled forwarded to EventLogger.
    logged_cancelled = [e for e in received if e.event_type == "tool_call_cancelled"]
    assert logged_cancelled, "tool_call_cancelled must be forwarded to EventLogger"
