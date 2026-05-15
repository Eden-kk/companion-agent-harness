"""v0.1c Task 1 — raw_video_frame ingest: per-modality causal chains are independent.

Success criterion:
  (a) interleaved audio+video chunks produce closed causal chains (no orphans);
  (b) a video frame's caused_by never points to an audio chunk id;
  (c) an audio chunk's caused_by never points to a video frame id.
"""

import hashlib
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from companion_harness.causal_graph import CausalGraph
from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.schemas import Event


def _wall() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_pcm(n: int) -> bytes:
    return bytes(range(256)) * 12 + bytes([n & 0xFF]) * 64


def _make_frame(n: int) -> bytes:
    # Minimal synthetic frame blob: 4-byte header + index byte
    return b"\x89PNG" + bytes([n & 0xFF]) * 16


@pytest.fixture()
def blob_dir(tmp_path: Path) -> Path:
    return tmp_path / "blobs"


@pytest.fixture()
def collected_events() -> list[Event]:
    return []


@pytest.fixture()
def logger(collected_events: list[Event]) -> EventLogger:
    async def sink(event: Event) -> None:
        collected_events.append(event)

    return EventLogger(sink)


@pytest.mark.asyncio
async def test_interleaved_audio_video_causal_chains(
    blob_dir: Path,
    collected_events: list[Event],
    logger: EventLogger,
) -> None:
    """Interleaved audio+video: no orphans, independent per-modality caused_by chains."""
    await logger.start()

    ingest = InputIngest(logger, blob_dir)
    session = ingest.open_session("test-client")

    base_ms = int(time.monotonic() * 1000)
    N = 4

    # Interleave: audio0, video0, audio1, video1, ...
    audio_events: list[Event] = []
    video_events: list[Event] = []
    for i in range(N):
        audio_meta = CaptureMetadata(
            client_id="test-client",
            timestamp_mono_ms=base_ms + i * 100,
            timestamp_wall=_wall(),
        )
        ae = ingest.ingest_chunk(session, _make_pcm(i), audio_meta)
        audio_events.append(ae)

        video_meta = CaptureMetadata(
            client_id="test-client",
            timestamp_mono_ms=base_ms + i * 100 + 50,
            timestamp_wall=_wall(),
        )
        ve = ingest.ingest_video_frame(session, _make_frame(i), video_meta)
        video_events.append(ve)

    await logger.stop()

    # (a) No orphan events — causal DAG must close across both modalities
    graph = CausalGraph(collected_events)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"orphan events: {report.orphan_event_ids}, dangling: {report.dangling_refs}"
    )

    # Collect all audio and video event ids for cross-modal checks
    audio_ids = {e.event_id for e in audio_events}
    video_ids = {e.event_id for e in video_events}

    # (b) A video frame's caused_by must never point to an audio chunk id
    for ve in video_events:
        for ref in ve.caused_by:
            assert ref not in audio_ids, (
                f"video frame {ve.event_id} caused_by audio chunk {ref}"
            )

    # (c) An audio chunk's caused_by must never point to a video frame id
    for ae in audio_events:
        for ref in ae.caused_by:
            assert ref not in video_ids, (
                f"audio chunk {ae.event_id} caused_by video frame {ref}"
            )

    # Verify modality-specific chain shapes
    session_open = next(e for e in collected_events if e.event_type == "session_open")

    # First audio chunk traces to session_open; subsequent to prior audio chunk
    assert audio_events[0].caused_by == [session_open.event_id]
    for prev, cur in zip(audio_events, audio_events[1:]):
        assert cur.caused_by == [prev.event_id]

    # First video frame traces to session_open; subsequent to prior video frame
    assert video_events[0].caused_by == [session_open.event_id]
    for prev, cur in zip(video_events, video_events[1:]):
        assert cur.caused_by == [prev.event_id]

    # Verify event_type and payload_kind on video events
    for ve in video_events:
        assert ve.event_type == "raw_video_frame"
        assert ve.payload_kind == "raw_video"

    # payload_hash matches stored blob
    for ve in video_events:
        assert ve.payload_ref is not None
        event_id = ve.payload_ref.removeprefix("blob://")
        stored = ingest.read_blob(event_id)
        assert hashlib.sha256(stored).hexdigest() == ve.payload_hash

    # seq_no is monotonically increasing across all events
    seq_nos = [e.seq_no for e in collected_events]
    assert seq_nos == sorted(seq_nos)
    assert len(set(seq_nos)) == len(seq_nos)
