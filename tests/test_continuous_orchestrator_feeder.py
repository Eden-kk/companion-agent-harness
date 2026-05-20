"""PR1 success-criterion test: ContinuousOrchestrator emits per-chunk events.

CPU-runnable (no GPU, no real weights). Injects FakeForegroundModel whose
stream_chunks() mirrors the real adapter: accumulates _CHUNK_SAMPLES (16000)
samples before yielding one record per full chunk. Asserts:
  - exactly n_full_chunks continuous_chunk_processed events emitted
  - every emitted event's caused_by[0] resolves to a logged raw_audio_chunk
    event_id (true DAG closure; invariant #1)
  - no TTS / assistant_audio_* events fire (PR1 is silence-only)

See subplan-turn-free-pr1.md §7.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.evals.scenarios.audio_feeder import DirectAudioInputFeeder
from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

# Mirror the real adapter constant (16 kHz PCM16: 1 s = 16000 samples = 32000 bytes).
_CHUNK_SAMPLES = 16_000
_BYTES_PER_SAMPLE = 2  # int16


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

    Faithfully mirrors the real adapter: accumulates incoming (bytes, event_id)
    pairs into a float-sample buffer and yields ONE record only when the buffer
    reaches _CHUNK_SAMPLES samples.  Sub-chunk leftovers on sentinel are
    silence-padded and yielded as a final record (mirroring infer_stream
    tail-drain).  The caused_by_evt_id returned is the most-recent event_id
    consumed into that chunk (matching the real adapter).
    """

    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, None, str], None]:
        buf_samples = 0
        latest_evt_id: str = ""
        while True:
            pcm_bytes, evt_id = await audio_in.get()
            if pcm_bytes == b"" and evt_id == "":
                # drain tail (silence-pad) if any samples remain
                if buf_samples > 0 and latest_evt_id:
                    yield (True, "", None, latest_evt_id)
                return
            latest_evt_id = evt_id
            buf_samples += len(pcm_bytes) // _BYTES_PER_SAMPLE
            while buf_samples >= _CHUNK_SAMPLES:
                buf_samples -= _CHUNK_SAMPLES
                yield (True, "", None, latest_evt_id)


class _NullAudioOutput:
    """Minimal audio_output stub (PR3a required arg). This model always listens
    (silence), so the orchestrator never starts speech — never dereferenced."""

    @property
    def is_playing(self) -> bool:
        return False

    def start_generation(self, *, caused_by: list[str]) -> str:
        return "noop"


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_continuous_orchestrator_emits_per_chunk_events() -> None:
    """ContinuousOrchestrator emits exactly n_full_chunks events; DAG closed; no TTS."""
    logger, received = _make_logger()
    await logger.start()

    session_id = "cco-test-01"
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    orch = ContinuousOrchestrator(
        session_id=session_id,
        logger=logger,
        audio_in=audio_in,
        foreground_model=FakeForegroundModel(),
        audio_output=_NullAudioOutput(),
    )

    clock = SyntheticClock()
    feeder = DirectAudioInputFeeder(audio_in)

    # Feed 5 full 1-second chunks (32000 bytes each = 16000 int16 samples).
    n_chunks = 5
    chunk_bytes = b"\x00" * (_CHUNK_SAMPLES * _BYTES_PER_SAMPLE)
    chunks = [chunk_bytes] * n_chunks

    # Pre-compute the exact event_ids the feeder will generate (feeder uses
    # f"fixture-audio-{clock.now_ms()}-{i}" and advances clock 20ms per chunk),
    # log a real raw_audio_chunk Event for each so caused_by[] can close the DAG
    # (invariant #1).
    logged_audio_event_ids: set[str] = set()
    feeder_event_ids: list[str] = []
    t_ms = clock.now_ms()
    for i in range(n_chunks):
        eid = f"fixture-audio-{t_ms}-{i}"
        feeder_event_ids.append(eid)
        logged_audio_event_ids.add(eid)
        t_ms += 20  # mirror feeder's clock.advance_ms(20)

    for seq_i, (eid, pcm) in enumerate(zip(feeder_event_ids, chunks)):
        raw_evt = Event(
            event_id=eid,
            session_id=session_id,
            schema_version="0.1",
            seq_no=seq_i + 1,
            event_type="raw_audio_chunk",
            timestamp_mono_ms=int(eid.split("-")[2]),
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source="fixture.audio",
            caused_by=[session_id + "-open"],
            payload_hash=hashlib.sha256(pcm).hexdigest()[:16],
            payload_ref=None,
            payload_kind="raw_audio",
            subject_class="self",
            sensitivity="sensitive",
            retention_policy_id="default",
        )
        logger.log(raw_evt)

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

    # Exactly n_chunks continuous_chunk_processed events (one per full 1-second chunk)
    assert len(chunk_events) == n_chunks, (
        f"expected {n_chunks} chunk events, got {len(chunk_events)}"
    )

    # Every emitted continuous_chunk_processed event's caused_by[0] resolves to
    # a logged raw_audio_chunk event_id (true DAG closure; invariant #1)
    for evt in chunk_events:
        assert evt.caused_by, (
            f"orphan event {evt.event_id} ({evt.event_type}) has empty caused_by"
        )
        assert evt.caused_by[0] in logged_audio_event_ids, (
            f"event {evt.event_id} caused_by[0]={evt.caused_by[0]!r} not in logged "
            f"raw_audio_chunk ids {logged_audio_event_ids}"
        )

    # No TTS / audio output events (PR1 is silence-only)
    assert not tts_events, f"unexpected TTS events: {[e.event_type for e in tts_events]}"
