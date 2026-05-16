"""F1b fix — MiniCPMAddressingClassifier wired by server.

Three tests:
  1. build_live_pipeline with minicpm_text_model → uses MiniCPMAddressingClassifierImpl.
  2. build_live_pipeline without minicpm_text_model → uses _NullMiniCPMAddressingClassifier
     AND emits a signal_producer_fallback event once at session open.
  3. build_app passes foreground_model as minicpm_text_model → pipeline uses Impl not Null.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio

from companion_harness.addressing_classifier import (
    MiniCPMAddressingClassifierImpl,
    _NullMiniCPMAddressingClassifier,
)
from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import InputIngest
from companion_harness.schemas import Event, MemoryItem, ThinkerProposal
from manual_test_console.live_pipeline import build_live_pipeline


class _FakeStreamingModel:
    """Minimal StreamingDuplexModel + chat() stub (doubles as minicpm_text_model)."""

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        return None

    def chat(self, prompt: str, max_new_tokens: int = 4) -> str:
        return "yes"


async def _drain(_event: Event) -> None:
    return None


@pytest_asyncio.fixture
async def _logger():
    logger = EventLogger(_drain, maxsize=64)
    await logger.start()
    try:
        yield logger
    finally:
        await logger.stop()


def _open_session(logger: EventLogger, tmp_path: Path):
    ingest = InputIngest(logger, tmp_path / "blobs")
    return ingest.open_session("test-client")


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_live_pipeline_uses_impl_when_model_provided(
    _logger: EventLogger, tmp_path: Path
) -> None:
    """build_live_pipeline(minicpm_text_model=<model>) wires MiniCPMAddressingClassifierImpl."""
    session = _open_session(_logger, tmp_path)
    model = _FakeStreamingModel()
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=model,
        minicpm_text_model=model,
        use_stubs=True,
    )
    classifier = pipeline.orchestrator._minicpm_addressing_classifier
    assert isinstance(classifier, MiniCPMAddressingClassifierImpl), (
        f"Expected MiniCPMAddressingClassifierImpl, got {type(classifier).__name__}"
    )


@pytest.mark.asyncio
async def test_build_live_pipeline_null_emits_fallback_event(
    tmp_path: Path,
) -> None:
    """build_live_pipeline without minicpm_text_model → _Null + signal_producer_fallback emitted once."""
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    logger = EventLogger(sink, maxsize=256)
    await logger.start()
    try:
        session = _open_session(logger, tmp_path)
        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            minicpm_text_model=None,
            use_stubs=True,
        )
        classifier = pipeline.orchestrator._minicpm_addressing_classifier
        assert isinstance(classifier, _NullMiniCPMAddressingClassifier), (
            f"Expected _NullMiniCPMAddressingClassifier, got {type(classifier).__name__}"
        )
        # Drain the logger queue so sink receives the startup event.
        import asyncio
        await asyncio.sleep(0.05)
        fallback_evts = [
            e for e in received
            if e.event_type == "signal_producer_fallback"
            and e.source == "addressing_classifier"
        ]
        assert len(fallback_evts) == 1, (
            f"Expected exactly one startup signal_producer_fallback, got {len(fallback_evts)}"
        )
        pl = fallback_evts[0].payload_inline or {}
        assert pl.get("primary_producer") == "MiniCPMAddressingClassifier"
        assert pl.get("fallback_producer") == "WakeWordAddressingClassifier"
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_server_build_app_passes_minicpm_text_model(tmp_path: Path) -> None:
    """build_app passes foreground_model as minicpm_text_model to build_live_pipeline."""
    import asyncio
    from unittest.mock import patch
    from aiohttp import ClientSession
    from aiohttp.test_utils import TestServer
    import manual_test_console.server as server_mod
    from manual_test_console.server import build_app

    captured_kwargs: list[dict] = []
    _real_build = server_mod.build_live_pipeline

    def _spy_build(**kwargs):
        captured_kwargs.append(kwargs)
        return _real_build(**kwargs)

    model = _FakeStreamingModel()
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=model,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        ws_base = f"ws://{server.host}:{server.port}"
        # Patch must be active when the WS handler runs (inside start_server scope).
        with patch.object(server_mod, "build_live_pipeline", side_effect=_spy_build):
            async with ClientSession() as sess:
                ingest_ws = await sess.ws_connect(f"{ws_base}/ws/ingest")
                await asyncio.sleep(0.15)
                await ingest_ws.close()
    finally:
        await server.close()

    assert captured_kwargs, "build_live_pipeline was never called"
    kwargs = captured_kwargs[0]
    assert kwargs.get("minicpm_text_model") is model, (
        "server.py must pass foreground_model as minicpm_text_model to build_live_pipeline"
    )
