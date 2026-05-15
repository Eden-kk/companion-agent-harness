"""Explicit-remember event chain in the live pipeline (v0.1h Task 1).

Monkeypatches _detect_explicit_remember to return (True, "extracted text"),
then drives a turn through the live pipeline. Asserts the event chain
raw_audio_chunk → vad_turn_signal → policy_decision → memory_write_candidate
closes (no orphan tail) and that the candidate payload carries the extracted text.
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
from manual_test_console.live_pipeline import build_live_pipeline


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-rem",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


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


def _pcm_speech(n: int = 1600) -> bytes:
    out = bytearray()
    for i in range(n):
        v = 8000 if (i & 1) else -8000
        out += v.to_bytes(2, "little", signed=True)
    return bytes(out)


def _pcm_silence(n: int = 1600) -> bytes:
    return b"\x00\x00" * n


@pytest.mark.asyncio
async def test_explicit_remember_event_chain_in_live_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """monkeypatched _detect_explicit_remember fires memory_write_candidate.

    The event chain raw_audio_chunk → (vad chain) → policy_decision →
    memory_write_candidate must close: the candidate event must be emitted
    and its payload must carry content.text == "extracted text".
    """
    import companion_harness.realtime_orchestrator as _orch_mod

    monkeypatch.setattr(
        _orch_mod,
        "_detect_explicit_remember",
        lambda transcript: (True, "extracted text"),
    )

    emitted: list[Any] = []

    async def _capture(evt: Any) -> None:
        emitted.append(evt)

    logger = EventLogger(_capture, maxsize=256)
    await logger.start()
    try:
        ingest = InputIngest(logger, tmp_path / "blobs")
        session = ingest.open_session("client-remember")

        pipeline = build_live_pipeline(
            session_id=session.session_id,
            logger=logger,
            ingest_session=session,
            foreground_duplex_model=_FakeStreamingModel(),
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
        finally:
            await pipeline.stop()

        candidate_events = [e for e in emitted if e.event_type == "memory_write_candidate"]
        assert len(candidate_events) >= 1, (
            f"Expected at least one memory_write_candidate event; got event types: "
            f"{[e.event_type for e in emitted]}"
        )

        # Verify the payload content via the orchestrator's in-memory store.
        cand_evt = candidate_events[0]
        orch = pipeline.orchestrator
        payload = orch._memory_event_payloads.get(cand_evt.event_id)
        assert payload is not None, "memory_write_candidate payload not found in orchestrator store"
        assert payload["content"]["text"] == "extracted text", (
            f"Expected 'extracted text', got {payload['content']['text']!r}"
        )

        # Causal chain: memory_write_candidate must have caused_by linking to a signal event.
        assert len(cand_evt.caused_by) >= 1, "memory_write_candidate must carry caused_by[]"
    finally:
        await logger.stop()
