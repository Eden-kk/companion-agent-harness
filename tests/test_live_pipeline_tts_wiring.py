"""Contract tests for KokoroTtsAdapter wiring into the live-pipeline factory.

Asserts (without loading Kokoro / GPU models):
  1. `build_live_pipeline(tts_adapter=<real>)` wires the injected adapter
     through to the orchestrator and exposes it on LivePipeline.tts_adapter
     (i.e. NOT NoopTtsAdapter).
  2. `use_stubs=True` forces NoopTtsAdapter even if a real adapter was passed
     (stub mode must never load real audio plumbing).
  3. `build_app(..., tts_adapter=<fake>)` makes that adapter the session-level
     adapter on /ws/ingest sessions.
  4. `/healthz` exposes `tts_model` (label reflects "stub:NoopTtsAdapter" when
     no real adapter is wired).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import InputIngest
from companion_harness.schemas import MemoryItem, ThinkerProposal
from manual_test_console.live_pipeline import NoopTtsAdapter, build_live_pipeline
from manual_test_console.server import KEY_TTS_ADAPTER, build_app


class _FakeStreamingModel:
    """Minimal StreamingDuplexModel fake — never yields a proposal."""

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        return None

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
            yield  # pragma: no cover

        return _gen()


class _FakeTtsAdapter:
    """TtsAdapter Protocol satisfier — fixed scripted chunks, no I/O."""

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        for c in [b"\x01\x02", b"\x03\x04"]:
            yield c


async def _drain_sink(_event: Any) -> None:
    return None


@pytest.mark.asyncio
async def test_build_live_pipeline_wires_injected_tts_adapter(tmp_path: Path) -> None:
    """When use_stubs=False and a real tts_adapter is injected, the factory
    must use it (not silently fall back to NoopTtsAdapter)."""
    logger = EventLogger(_drain_sink, maxsize=64)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("test-client")

        real = _FakeTtsAdapter()
        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=False,
            tts_adapter=real,
        )

        assert pipeline.tts_adapter is real
        assert not isinstance(pipeline.tts_adapter, NoopTtsAdapter)
        # The orchestrator must hold the same instance — the wiring is what
        # makes the voice-back loop actually fire audio bytes.
        assert pipeline.orchestrator._tts_adapter is real
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_build_live_pipeline_use_stubs_forces_noop(tmp_path: Path) -> None:
    """`use_stubs=True` MUST override an injected real adapter with NoopTtsAdapter."""
    logger = EventLogger(_drain_sink, maxsize=64)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("test-client")

        real = _FakeTtsAdapter()
        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            tts_adapter=real,
        )
        assert isinstance(pipeline.tts_adapter, NoopTtsAdapter)
        assert isinstance(pipeline.orchestrator._tts_adapter, NoopTtsAdapter)
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_build_live_pipeline_defaults_to_noop_when_none(tmp_path: Path) -> None:
    """Backward-compat: omitting tts_adapter still yields NoopTtsAdapter."""
    logger = EventLogger(_drain_sink, maxsize=64)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("test-client")

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=False,
        )
        assert isinstance(pipeline.tts_adapter, NoopTtsAdapter)
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_build_app_with_injected_tts_adapter_exposes_it(tmp_path: Path) -> None:
    """`build_app(tts_adapter=fake)` stores it on app state for the factory."""
    fake = _FakeTtsAdapter()
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeStreamingModel(),
        tts_adapter=fake,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        assert app[KEY_TTS_ADAPTER] is fake
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_healthz_reports_tts_model_field(tmp_path: Path) -> None:
    """`/healthz` returns the `tts_model` label."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        foreground_model=None,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/healthz")
            body = await resp.json()
            assert "tts_model" in body
            # No real adapter injected → stub label.
            assert body["tts_model"] == "stub:NoopTtsAdapter"
    finally:
        await server.close()
