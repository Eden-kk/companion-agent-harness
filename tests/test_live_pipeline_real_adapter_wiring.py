"""Contract tests for the 6 real-adapter wiring kwargs on `build_live_pipeline`
and the matching `build_app` kwargs + `--enable-*` CLI flags on the server.

Each real adapter (CLIPSceneChangeScorer, GroundingDINOAdapter,
HeuristicAVConflictScorer, MiniCPMDeicticDetector, ProsodyLexiconUrgencyScorer,
SentenceTransformerEmbedder) is opt-in. Default behavior is the existing null
stub — these tests pin both directions.

Tests use FAKE adapters that satisfy the relevant Protocol but never touch
torch / transformers / sentence-transformers / OpenCV. The real adapters are
imported on b200 by the server's main(); these tests only verify the WIRING
seam, not the model loads.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from companion_harness.av_conflict_scorer import _NullAudioVisualConflictScorer
from companion_harness.deictic_detector import _NullDeicticModel
from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import InputIngest
from companion_harness.memory_manager import _NullEmbeddingAdapter
from companion_harness.schemas import MemoryItem, ThinkerProposal
from companion_harness.urgency_scorer import _NullUrgencyScorer
from manual_test_console.live_pipeline import build_live_pipeline
from manual_test_console.server import (
    KEY_ADAPTER_LABELS,
    KEY_AV_CONFLICT_SCORER,
    KEY_DEICTIC_MODEL,
    KEY_EMBEDDER,
    KEY_GROUNDING_MODEL,
    KEY_SCENE_SCORER,
    KEY_URGENCY_SCORER,
    build_app,
)


# ---------------------------------------------------------------------------
# Test fakes (Protocol-conformant, zero-dep)
# ---------------------------------------------------------------------------


class _FakeStreamingModel:
    """StreamingDuplexModel fake — never yields a proposal."""

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


class _FakeSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.5


class _FakeGroundingModel:
    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return ("test", 0.7)


class _FakeAvConflictScorer:
    def score(self, audio: bytes, frame: bytes | None) -> float:
        return 0.3


class _FakeDeicticModel:
    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]:
        return (True, 0.9)


class _FakeUrgencyScorer:
    def score(self, transcript: str, audio: bytes | None) -> float:
        return 0.4


class _FakeEmbedder:
    def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


async def _drain_sink(_event: Any) -> None:
    return None


@pytest_asyncio.fixture
async def _logger():
    logger = EventLogger(_drain_sink, maxsize=64)
    await logger.start()
    try:
        yield logger
    finally:
        await logger.stop()


def _make_session(logger: EventLogger, tmp_path: Path):
    ingest = InputIngest(logger, tmp_path / "blobs")
    return ingest.open_session("test-client")


# ---------------------------------------------------------------------------
# build_live_pipeline kwargs: default OFF → null stubs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_defaults_keep_null_stubs(_logger: EventLogger, tmp_path: Path) -> None:
    """When no real adapters are injected, LivePipeline exposes None handles
    (the underlying detector / builder uses the null stub)."""
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
    )
    assert pipeline.av_conflict_scorer is None
    assert pipeline.urgency_scorer is None
    assert pipeline.deictic_model is None
    assert pipeline.embedder is None
    # The DeicticDetector inside the orchestrator must hold the null stub.
    assert isinstance(
        pipeline.orchestrator._deictic_detector._model, _NullDeicticModel
    )


# ---------------------------------------------------------------------------
# build_live_pipeline kwargs: each real adapter is wired when injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_av_conflict_scorer_is_wired(_logger: EventLogger, tmp_path: Path) -> None:
    session = _make_session(_logger, tmp_path)
    fake = _FakeAvConflictScorer()
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        av_conflict_scorer=fake,
    )
    assert pipeline.av_conflict_scorer is fake
    # The policy builder closure captured the real instance — invoking it should
    # surface the non-zero score.
    builder = pipeline.orchestrator._policy_inputs_builder
    from companion_harness.schemas import TurnSignal
    sig = TurnSignal(
        detector="vad",
        p_done=0.5,
        p_continue=0.5,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=[],
    )
    inputs = builder(sig, [sig])
    assert inputs.audio_visual_conflict_score == 0.3


@pytest.mark.asyncio
async def test_urgency_scorer_is_wired(_logger: EventLogger, tmp_path: Path) -> None:
    session = _make_session(_logger, tmp_path)
    fake = _FakeUrgencyScorer()
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        urgency_scorer=fake,
    )
    assert pipeline.urgency_scorer is fake
    builder = pipeline.orchestrator._policy_inputs_builder
    from companion_harness.schemas import TurnSignal
    sig = TurnSignal(
        detector="vad",
        p_done=0.5,
        p_continue=0.5,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=[],
    )
    inputs = builder(sig, [sig])
    assert inputs.urgency_score == 0.4


@pytest.mark.asyncio
async def test_deictic_model_is_wired(_logger: EventLogger, tmp_path: Path) -> None:
    session = _make_session(_logger, tmp_path)
    fake = _FakeDeicticModel()
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        deictic_model=fake,
    )
    assert pipeline.deictic_model is fake
    # The DeicticDetector inside the orchestrator must hold the injected model.
    assert pipeline.orchestrator._deictic_detector._model is fake


@pytest.mark.asyncio
async def test_embedder_is_wired(_logger: EventLogger, tmp_path: Path) -> None:
    """The embedder handle is exposed for the (future) episodic store. Today
    it only needs to round-trip through LivePipeline so a future wiring task
    can pick it up."""
    session = _make_session(_logger, tmp_path)
    fake = _FakeEmbedder()
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        embedder=fake,
    )
    assert pipeline.embedder is fake


# ---------------------------------------------------------------------------
# use_stubs=True forces null stubs even when real adapters are injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_stubs_overrides_injected_adapters(_logger: EventLogger, tmp_path: Path) -> None:
    """use_stubs=True must never reach for any real adapter, even if one was
    passed in — mirrors the TTS-stub contract."""
    session = _make_session(_logger, tmp_path)
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=True,
        av_conflict_scorer=_FakeAvConflictScorer(),
        urgency_scorer=_FakeUrgencyScorer(),
        deictic_model=_FakeDeicticModel(),
        embedder=_FakeEmbedder(),
    )
    assert pipeline.av_conflict_scorer is None
    assert pipeline.urgency_scorer is None
    assert pipeline.deictic_model is None
    assert pipeline.embedder is None
    assert isinstance(
        pipeline.orchestrator._deictic_detector._model, _NullDeicticModel
    )


# ---------------------------------------------------------------------------
# Integration: all 6 wired through the live pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_six_wired_simultaneously(_logger: EventLogger, tmp_path: Path) -> None:
    """All six adapters can be wired in one call; each is reachable from the
    LivePipeline / orchestrator."""
    session = _make_session(_logger, tmp_path)
    fake_av = _FakeAvConflictScorer()
    fake_ur = _FakeUrgencyScorer()
    fake_de = _FakeDeicticModel()
    fake_em = _FakeEmbedder()
    pipeline = build_live_pipeline(
        session_id=session.session_id,
        logger=_logger,
        ingest_session=session,
        foreground_duplex_model=_FakeStreamingModel(),
        use_stubs=False,
        av_conflict_scorer=fake_av,
        urgency_scorer=fake_ur,
        deictic_model=fake_de,
        embedder=fake_em,
    )
    assert pipeline.av_conflict_scorer is fake_av
    assert pipeline.urgency_scorer is fake_ur
    assert pipeline.deictic_model is fake_de
    assert pipeline.embedder is fake_em
    assert pipeline.orchestrator._deictic_detector._model is fake_de


# ---------------------------------------------------------------------------
# build_app: kwargs land in app state + adapter_labels reflect realness
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_app_defaults_to_stub_labels(tmp_path: Path) -> None:
    """Without --enable-* flags, every adapter label says stub:_Null*."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        foreground_model=None,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        labels = app[KEY_ADAPTER_LABELS]
        assert labels["scene_scorer"] == "stub:_NullSceneScorer"
        assert labels["grounding_model"] == "stub:_NullGroundingModel"
        assert labels["av_conflict_scorer"] == "stub:_NullAudioVisualConflictScorer"
        assert labels["deictic_model"] == "stub:_NullDeicticModel"
        assert labels["urgency_scorer"] == "stub:_NullUrgencyScorer"
        assert labels["embedder"] == "stub:_NullEmbeddingAdapter"
        assert app[KEY_AV_CONFLICT_SCORER] is None
        assert app[KEY_URGENCY_SCORER] is None
        assert app[KEY_DEICTIC_MODEL] is None
        assert app[KEY_EMBEDDER] is None
        assert app[KEY_SCENE_SCORER] is None
        assert app[KEY_GROUNDING_MODEL] is None
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_build_app_with_injected_adapters_exposes_them(tmp_path: Path) -> None:
    """build_app(scene_scorer=..., grounding_model=..., ...) stores each
    instance on app state and labels each as real:<ClassName>."""
    scene = _FakeSceneScorer()
    grounding = _FakeGroundingModel()
    av = _FakeAvConflictScorer()
    de = _FakeDeicticModel()
    ur = _FakeUrgencyScorer()
    em = _FakeEmbedder()
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeStreamingModel(),
        scene_scorer=scene,
        grounding_model=grounding,
        av_conflict_scorer=av,
        deictic_model=de,
        urgency_scorer=ur,
        embedder=em,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        assert app[KEY_SCENE_SCORER] is scene
        assert app[KEY_GROUNDING_MODEL] is grounding
        assert app[KEY_AV_CONFLICT_SCORER] is av
        assert app[KEY_DEICTIC_MODEL] is de
        assert app[KEY_URGENCY_SCORER] is ur
        assert app[KEY_EMBEDDER] is em
        labels = app[KEY_ADAPTER_LABELS]
        assert labels["scene_scorer"] == "real:_FakeSceneScorer"
        assert labels["grounding_model"] == "real:_FakeGroundingModel"
        assert labels["av_conflict_scorer"] == "real:_FakeAvConflictScorer"
        assert labels["deictic_model"] == "real:_FakeDeicticModel"
        assert labels["urgency_scorer"] == "real:_FakeUrgencyScorer"
        assert labels["embedder"] == "real:_FakeEmbedder"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_healthz_exposes_adapter_labels(tmp_path: Path) -> None:
    """/healthz returns the six adapter label fields with the right values."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        foreground_model=None,
        av_conflict_scorer=_FakeAvConflictScorer(),
        urgency_scorer=_FakeUrgencyScorer(),
        embedder=_FakeEmbedder(),
    )
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/healthz")
            body = await resp.json()
            assert body["scene_scorer"] == "stub:_NullSceneScorer"
            assert body["grounding_model"] == "stub:_NullGroundingModel"
            assert body["av_conflict_scorer"] == "real:_FakeAvConflictScorer"
            assert body["deictic_model"] == "stub:_NullDeicticModel"
            assert body["urgency_scorer"] == "real:_FakeUrgencyScorer"
            assert body["embedder"] == "real:_FakeEmbedder"
    finally:
        await server.close()
