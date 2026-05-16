"""SleepTimeAgent live-pipeline wiring (v0.1j Task 12).

Tests:
  test_sleep_time_agent_constructed_per_session
  test_sleep_time_agent_subscribes_to_event_stream
  test_sleep_time_agent_calls_memory_manager_commit
  test_sleep_time_agent_does_not_block_foreground
  test_unavailable_provenance_defaults_marker
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.schemas import MemoryItem, ThinkerProposal
from companion_harness.sleep_time_agent import SleepTimeAgent
from manual_test_console.live_pipeline import build_live_pipeline


class _FakeStreamingModel:
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
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            return
            yield  # pragma: no cover

        return _gen()


async def _drain(_evt: object) -> None:
    return None


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-sta",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


def _pcm_speech(n: int = 1600) -> bytes:
    out = bytearray()
    for i in range(n):
        v = 8000 if (i & 1) else -8000
        out += v.to_bytes(2, "little", signed=True)
    return bytes(out)


def _pcm_silence(n: int = 1600) -> bytes:
    return b"\x00\x00" * n


@pytest.mark.asyncio
async def test_sleep_time_agent_constructed_per_session(tmp_path: Path) -> None:
    """wire_sleep_time_agent=True yields a per-session SleepTimeAgent instance."""
    logger = EventLogger(_drain, maxsize=64)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session_a = ingest.open_session("client-a")
        session_b = ingest.open_session("client-b")

        pipeline_a = build_live_pipeline(
            session_id=session_a.session_id,
            logger=logger,
            ingest_session=session_a,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            wire_sleep_time_agent=True,
        )
        pipeline_b = build_live_pipeline(
            session_id=session_b.session_id,
            logger=logger,
            ingest_session=session_b,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            wire_sleep_time_agent=True,
        )

        assert pipeline_a.sleep_time_agent is not None
        assert pipeline_b.sleep_time_agent is not None
        assert pipeline_a.sleep_time_agent is not pipeline_b.sleep_time_agent
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_sleep_time_agent_subscribes_to_event_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SleepTimeAgent subscribes to the event stream and receives memory_write_candidate events."""
    import companion_harness.realtime_orchestrator as _orch_mod

    emitted: list[Any] = []

    async def _capture(evt: Any) -> None:
        emitted.append(evt)

    monkeypatch.setattr(
        _orch_mod,
        "_detect_explicit_remember",
        lambda transcript: (True, "wired text"),
    )

    logger = EventLogger(_capture, maxsize=256)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client-sub")

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            wire_sleep_time_agent=True,
        )
        assert pipeline.sleep_time_agent is not None

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
        finally:
            await pipeline.stop()

        candidate_events = [e for e in emitted if e.event_type == "memory_write_candidate"]
        assert len(candidate_events) >= 1, (
            f"Expected at least one memory_write_candidate; got {[e.event_type for e in emitted]}"
        )
        # SleepTimeAgent must have processed the candidate (commit or skip emitted).
        followup = [
            e for e in emitted
            if e.event_type in ("memory_commit_completed", "memory_commit_skipped")
        ]
        assert len(followup) >= 1, (
            "Expected memory_commit_completed or memory_commit_skipped from SleepTimeAgent"
        )
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_sleep_time_agent_calls_memory_manager_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SleepTimeAgent calls commit() on the matching store with a MemoryItem carrying provenance fields."""
    import companion_harness.realtime_orchestrator as _orch_mod

    committed: list[MemoryItem] = []

    class _CapturingStore:
        def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
            committed.append(item)

        def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
            return []

        def forget(self, item_id: str) -> None:
            return None

        def hard_delete(self, item_id: str) -> None:
            return None

    monkeypatch.setattr(
        _orch_mod,
        "_detect_explicit_remember",
        lambda transcript: (True, "commit text"),
    )

    logger = EventLogger(_drain, maxsize=128)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client-commit")

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            wire_sleep_time_agent=True,
        )
        assert pipeline.sleep_time_agent is not None

        # Replace the episodic store with a capturing one before start().
        capturing = _CapturingStore()
        pipeline.episodic_store = capturing
        pipeline.sleep_time_agent._stores["episodic"] = capturing  # type: ignore[index]

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
        finally:
            await pipeline.stop()

        assert len(committed) >= 1, "Expected at least one commit() call"
        item = committed[0]
        assert item.source_event_id
        assert item.created_at
        assert item.confidence is not None
        assert item.salience is not None
        assert item.user_visible_summary is not None
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_sleep_time_agent_does_not_block_foreground(tmp_path: Path) -> None:
    """SleepTimeAgent runs on its own subscribed callback; start() returns without blocking."""
    logger = EventLogger(_drain, maxsize=64)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client-noblock")

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            wire_sleep_time_agent=True,
        )

        # start() must return promptly — no blocking call on the realtime path.
        await asyncio.wait_for(pipeline.start(), timeout=2.0)
        await pipeline.stop()
    finally:
        await logger.stop()


def test_unavailable_provenance_defaults_marker() -> None:
    """UNAVAILABLE: #188 markers must exist in sleep_time_agent.py for stub provenance."""
    import inspect
    import companion_harness.sleep_time_agent as _sta_mod

    source = inspect.getsource(_sta_mod)
    assert "UNAVAILABLE: #188" in source, (
        "Expected '# UNAVAILABLE: #188' marker(s) in sleep_time_agent.py "
        "for stub confidence/salience defaults"
    )
