"""Per-session memory store isolation (v0.1h Task 1).

Each LivePipeline built by build_live_pipeline() must own distinct store
object identities — cross-session memory leakage is forbidden.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import InputIngest
from companion_harness.schemas import MemoryItem, ThinkerProposal
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


@pytest.mark.asyncio
async def test_per_session_memory_isolation(tmp_path: Path) -> None:
    """Two LivePipeline instances must own distinct store object identities."""
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
            blob_dir=tmp_path / "blobs",
        )
        pipeline_b = build_live_pipeline(
            session_id=session_b.session_id,
            logger=logger,
            ingest_session=session_b,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            blob_dir=tmp_path / "blobs",
        )

        assert id(pipeline_a.session_state_store) != id(pipeline_b.session_state_store)
        assert id(pipeline_a.core_store) != id(pipeline_b.core_store)
        assert id(pipeline_a.episodic_store) != id(pipeline_b.episodic_store)
        assert id(pipeline_a.semantic_store) != id(pipeline_b.semantic_store)
    finally:
        await logger.stop()


@pytest.mark.asyncio
async def test_per_session_memory_dirs_created(tmp_path: Path) -> None:
    """blob_dir causes the four per-session memory subdirs to be created."""
    logger = EventLogger(_drain, maxsize=64)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client-x")

        build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
            use_stubs=True,
            blob_dir=tmp_path / "blobs",
        )

        mem_root = tmp_path / "blobs" / session.session_id / "memory"
        for subdir in ("session", "core", "episodic", "semantic"):
            assert (mem_root / subdir).is_dir(), f"missing {subdir}"
    finally:
        await logger.stop()
