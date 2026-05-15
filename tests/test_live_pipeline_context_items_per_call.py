"""context_items flow T2 → _pending_retrieved_items → T3 → infer_stream (v0.1h Task 1).

Verifies the per-call context_items path end-to-end:
- _pending_retrieved_items is set in T2 before _batch_open_event.set()
- T3 passes context_items=tuple(_pending_retrieved_items) to process_stream
- _pending_retrieved_items is cleared in T4 after _batch_close_event.set()
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.schemas import (
    MemoryItem,
    SensitiveField,
    ThinkerProposal,
)
from manual_test_console.live_pipeline import build_live_pipeline


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-ctx",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


def _make_item(item_id: str) -> MemoryItem:
    now = datetime.now(timezone.utc).isoformat()
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"text": "test content"},
        source_event_id="src-1",
        created_at=now,
        last_confirmed_at=now,
        confidence=0.9,
        salience=0.8,
        valid_from=now,
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value=f"summary for {item_id}",
        ),
        privacy_level="user_content",
        mutability="user_only",
    )


class _RecordingStreamingModel:
    """StreamingDuplexModel that records every context_items tuple it receives."""

    def __init__(self) -> None:
        self.recorded_context: list[tuple[MemoryItem, ...]] = []

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        return None

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple[MemoryItem, ...] = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        self.recorded_context.append(context_items)

        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            return
            yield  # pragma: no cover

        return _gen()


class _SingleItemStore:
    """MemoryManager that always returns one fixed item from retrieve()."""

    def __init__(self, item: MemoryItem) -> None:
        self._item = item

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        return None

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        return [self._item]

    def forget(self, item_id: str) -> None:
        return None

    def hard_delete(self, item_id: str) -> None:
        return None


async def _drain(_evt: object) -> None:
    return None


def _pcm_speech(n: int = 1600) -> bytes:
    out = bytearray()
    for i in range(n):
        v = 8000 if (i & 1) else -8000
        out += v.to_bytes(2, "little", signed=True)
    return bytes(out)


def _pcm_silence(n: int = 1600) -> bytes:
    return b"\x00\x00" * n


@pytest.mark.asyncio
async def test_context_items_flow_t2_to_t3(tmp_path: Path) -> None:
    """_pending_retrieved_items is set in T2 before _batch_open_event fires.

    Directly simulates the T2→T3 handoff: set _pending_retrieved_items on the
    orchestrator, then fire _batch_open_event and run T3 one iteration. Asserts
    that context_items reaches the recording model.
    """
    from companion_harness.audio_output_controller import AudioOutputController
    from companion_harness.backchannel_classifier import BackchannelClassifier
    from companion_harness.foreground_model import ForegroundModel
    from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
    from companion_harness.tts_adapter import SilentTtsAdapter
    from companion_harness.turn_detector_smart import SmartTurnDetector
    from companion_harness.turn_detector_vad import VADDetector
    from companion_harness.addressing_classifier import WakeWordAddressingClassifier
    from manual_test_console.live_pipeline import (
        EnergyVADModel,
        SharedLoggerProxy,
        SilenceSmartTurnModel,
        ZeroBackchannelModel,
    )

    item = _make_item("item-001")
    recording_model = _RecordingStreamingModel()

    logger = EventLogger(_drain, maxsize=128)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client-ctx")

        audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

        async def _noop_sink(chunk: bytes) -> None:
            return None

        shielded = SharedLoggerProxy(logger)  # type: ignore[arg-type]
        vad = VADDetector(model=EnergyVADModel(), session_id=session.session_id, logger=shielded, speech_threshold=0.5, silence_onset_ms=300, frame_duration_ms=32)  # type: ignore[arg-type]
        st = SmartTurnDetector(model=SilenceSmartTurnModel(), session_id=session.session_id, logger=shielded)  # type: ignore[arg-type]
        bc = BackchannelClassifier(model=ZeroBackchannelModel(), session_id=session.session_id, logger=shielded)  # type: ignore[arg-type]
        fg = ForegroundModel(model=recording_model, session_id=session.session_id, logger=shielded)  # type: ignore[arg-type]
        aoc = AudioOutputController(session_id=session.session_id, logger=shielded, sink=_noop_sink)  # type: ignore[arg-type]

        from companion_harness.schemas import PolicyInputs, TurnSignal

        def _builder(signal: TurnSignal, history: list[TurnSignal]) -> PolicyInputs:
            return PolicyInputs(
                user_speaking=False, eou_probability=0.5, assistant_speaking=False,
                scene_change_score=0.0, deictic_reference=False, user_addressed_agent=True,
                urgency_score=0.0, proactivity_budget_remaining={}, privacy_mode="normal",
                current_task_mode="normal", social_mode="user_addressing_agent",
                risk_mode="normal", cooldown_state={}, attachment_risk_level=0.0,
                audio_visual_conflict_score=0.0, grounding_confidence=1.0, deictic_ambiguous=False,
            )

        orch = StreamingRealtimeOrchestrator(
            session_id=session.session_id,
            logger=shielded,  # type: ignore[arg-type]
            ingest_session=session,
            audio_in=audio_in,
            vad_detector=vad,
            smart_turn_detector=st,
            backchannel_classifier=bc,
            policy_inputs_builder=_builder,
            foreground_model=fg,
            audio_output=aoc,
            tts_adapter=SilentTtsAdapter(chunk_count=1),
            addressing_classifier=WakeWordAddressingClassifier(),
        )

        # Directly simulate T2's handoff to T3: set _pending_retrieved_items,
        # then open the batch. T3 task must not be running yet.
        orch._pending_retrieved_items = [item]
        orch._batch_close_event.clear()
        orch._batch_open_event.set()

        # Push one audio frame so T3's frame_iter yields one item and terminates
        # after batch close.
        chunk = _pcm_silence()
        await audio_in.put((chunk, "evt-001"))

        # Run T3 manually for one iteration.
        t3_task = asyncio.get_event_loop().create_task(
            orch._foreground_stream_task(), name="T3_test"
        )
        try:
            # Let T3 wake up, consume the batch, call process_stream.
            await asyncio.sleep(0.1)
            # Close the batch so T3's frame_iter drains.
            orch._batch_close_event.set()
            await asyncio.sleep(0.1)
        finally:
            t3_task.cancel()
            try:
                await t3_task
            except (asyncio.CancelledError, Exception):
                pass

        assert len(recording_model.recorded_context) >= 1, (
            f"Expected at least one infer_stream call; got {recording_model.recorded_context}"
        )
        # At least one call must carry the retrieved item.
        assert any(item in ctx for ctx in recording_model.recorded_context), (
            f"Expected item in at least one context_items call; got {recording_model.recorded_context}"
        )
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_pending_retrieved_items_cleared_after_batch(tmp_path: Path) -> None:
    """_pending_retrieved_items must be [] after T4 closes the batch."""
    recording_model = _RecordingStreamingModel()

    logger = EventLogger(_drain, maxsize=128)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client-clear")

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=recording_model,
            use_stubs=True,
        )

        await pipeline.start()
        try:
            for _ in range(20):
                chunk = _pcm_speech()
                evt = ingest.ingest_chunk(session, chunk, _meta())
                pipeline.push_audio(chunk, evt.event_id)
            for _ in range(15):
                chunk = _pcm_silence()
                evt = ingest.ingest_chunk(session, chunk, _meta())
                pipeline.push_audio(chunk, evt.event_id)

            await asyncio.sleep(0.5)

            # T4 has run by now; pending list must be cleared.
            assert pipeline.orchestrator._pending_retrieved_items == []
        finally:
            await pipeline.stop()
    finally:
        await logger.stop()
