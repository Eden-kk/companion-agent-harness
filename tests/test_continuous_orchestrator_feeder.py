"""PR1 success-criterion test: ContinuousOrchestrator emits per-chunk events.

CPU-runnable (no GPU, no real weights). Injects FakeForegroundModel whose
stream_chunks() yields one record per consumed chunk. Asserts:
  - >= 1 continuous_chunk_processed event per consumed chunk
  - every emitted event has non-empty caused_by[] (no orphans; invariant #1)
  - no TTS / assistant_audio_* events fire (PR1 is silence-only)

See subplan-turn-free-pr1.md §7.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.evals.scenarios.audio_feeder import DirectAudioInputFeeder
from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


class FakeForegroundModel:
    """Stub satisfying the stream_chunks() adapter interface.

    Yields (is_listen=True, text="", audio_kv_len=None, caused_by_evt_id)
    for every (non-sentinel) chunk consumed from audio_in.
    """

    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, None, str], None]:
        while True:
            pcm_bytes, evt_id = await audio_in.get()
            if pcm_bytes == b"" and evt_id == "":
                return
            yield (True, "", None, evt_id)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuous_orchestrator_emits_per_chunk_events() -> None:
    """ContinuousOrchestrator emits >= 1 event per chunk; no orphans; no TTS."""
    logger, received = _make_logger()
    await logger.start()

    session_id = "cco-test-01"
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    orch = ContinuousOrchestrator(
        session_id=session_id,
        logger=logger,
        audio_in=audio_in,
        foreground_model=FakeForegroundModel(),
    )

    clock = SyntheticClock()
    feeder = DirectAudioInputFeeder(audio_in)

    # Feed 5 chunks of 32-byte PCM16 silence
    n_chunks = 5
    chunks = [b"\x00" * 32] * n_chunks
    feeder.feed(chunks, clock)

    # Push sentinel to terminate run()
    audio_in.put_nowait((b"", ""))

    await orch.run()
    await logger.stop()

    chunk_events = [e for e in received if e.event_type == "continuous_chunk_processed"]
    tts_events = [
        e for e in received
        if e.event_type.startswith("assistant_audio") or e.event_type.startswith("assistant_generation")
    ]

    # >= 1 continuous_chunk_processed per consumed chunk
    assert len(chunk_events) >= n_chunks, (
        f"expected >= {n_chunks} chunk events, got {len(chunk_events)}"
    )

    # Every emitted event has closed caused_by[] (invariant #1)
    for evt in received:
        assert evt.caused_by, (
            f"orphan event {evt.event_id} ({evt.event_type}) has empty caused_by"
        )

    # No TTS / audio output events (PR1 is silence-only)
    assert not tts_events, f"unexpected TTS events: {[e.event_type for e in tts_events]}"
