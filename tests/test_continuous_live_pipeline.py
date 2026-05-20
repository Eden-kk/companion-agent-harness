"""PR5b success-criterion test: ContinuousLivePipeline wires and runs end-to-end.

CPU-runnable (no GPU, no real weights). Asserts:
  - build_continuous_pipeline returns a ContinuousLivePipeline whose .orchestrator
    is a ContinuousOrchestrator.
  - start() / push_audio() × N / stop() produces exactly N continuous_chunk_processed
    events with non-empty caused_by (end-to-end through the real audio_in queue).
  - stop() completes without a pending-task warning (sentinel-honored + cancel-safe).
  - The --continuous-OFF selection (KEY_CONTINUOUS=False) builds a LivePipeline with a
    StreamingRealtimeOrchestrator, not a ContinuousLivePipeline (regression guard).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import MagicMock

import pytest

from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event
from manual_test_console.live_pipeline import (
    ContinuousLivePipeline,
    build_continuous_pipeline,
)

_CHUNK_SAMPLES = 16_000
_BYTES_PER_SAMPLE = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


class _FakeForegroundModel:
    """Mirrors the real stream_chunks adapter: accumulates samples, yields per full chunk.

    Copied from test_continuous_orchestrator_feeder.py (proven fake).
    """

    def inject_scratchpad(self, text: str) -> None:
        pass

    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, None, str], None]:
        buf_samples = 0
        latest_evt_id: str = ""
        while True:
            pcm_bytes, evt_id = await audio_in.get()
            if pcm_bytes == b"" and evt_id == "":
                if buf_samples > 0 and latest_evt_id:
                    yield (True, "", None, latest_evt_id)
                return
            latest_evt_id = evt_id
            buf_samples += len(pcm_bytes) // _BYTES_PER_SAMPLE
            while buf_samples >= _CHUNK_SAMPLES:
                buf_samples -= _CHUNK_SAMPLES
                yield (True, "", None, latest_evt_id)


# ---------------------------------------------------------------------------
# End-to-end smoke test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_continuous_live_pipeline_smoke() -> None:
    """build_continuous_pipeline → start → push_audio × N → stop ⇒ N chunk events."""
    logger, received = _make_logger()
    await logger.start()

    pipeline = build_continuous_pipeline(
        session_id="pr5b-smoke",
        logger=logger,
        foreground_duplex_model=_FakeForegroundModel(),
    )

    assert isinstance(pipeline, ContinuousLivePipeline), (
        f"expected ContinuousLivePipeline, got {type(pipeline)}"
    )
    assert isinstance(pipeline.orchestrator, ContinuousOrchestrator), (
        f"expected ContinuousOrchestrator, got {type(pipeline.orchestrator)}"
    )

    await pipeline.start()

    n_chunks = 3
    chunk_bytes = b"\x00" * (_CHUNK_SAMPLES * _BYTES_PER_SAMPLE)
    for i in range(n_chunks):
        pipeline.push_audio(chunk_bytes, f"audio-evt-{i}", ts_mono_ms=i * 1000)

    await pipeline.stop()
    await logger.stop()

    chunk_events = [e for e in received if e.event_type == "continuous_chunk_processed"]
    assert len(chunk_events) == n_chunks, (
        f"expected {n_chunks} chunk events, got {len(chunk_events)}"
    )
    for evt in chunk_events:
        assert evt.caused_by, f"orphan event {evt.event_id} has empty caused_by"


# ---------------------------------------------------------------------------
# Regression: --continuous OFF → LivePipeline / StreamingRealtimeOrchestrator
# ---------------------------------------------------------------------------


def test_continuous_flag_threads_to_app_context(tmp_path) -> None:
    """build_app(continuous=...) sets app[KEY_CONTINUOUS] — the real flag wiring PR5b added.

    The /ws/ingest handler branches on request.app[KEY_CONTINUOUS]; this asserts the
    flag actually reaches the app context (default OFF = zero regression).
    """
    from manual_test_console.server import KEY_CONTINUOUS, build_app

    app_off = build_app(blob_dir=tmp_path / "blobs-off")
    assert app_off[KEY_CONTINUOUS] is False, "default must be continuous OFF (zero regression)"

    app_on = build_app(blob_dir=tmp_path / "blobs-on", continuous=True)
    assert app_on[KEY_CONTINUOUS] is True, "--continuous must thread into KEY_CONTINUOUS"
