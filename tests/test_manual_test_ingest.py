"""Manual-test Task 1 — InputIngest contract test.

Success criterion: a scripted envelope stream fed through InputIngest produces
N raw_audio Events with closed caused_by[] chains (no orphans), valid
payload_kind="raw_audio", and payload_hash matching the stored blob bytes.
"""

import asyncio
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
    # 3200 bytes = 100 ms of PCM16 16 kHz mono (matching VisionClaw framing)
    return bytes(range(256)) * 12 + bytes([n & 0xFF]) * 64


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
async def test_manual_test_ingest_causal_chain_closed(
    blob_dir: Path,
    collected_events: list[Event],
    logger: EventLogger,
) -> None:
    """N raw_audio Events: no orphans, correct payload_kind, hash matches blob."""
    await logger.start()

    ingest = InputIngest(logger, blob_dir)
    session = ingest.open_session("test-client")

    N = 5
    chunks = [_make_pcm(i) for i in range(N)]
    for i, pcm in enumerate(chunks):
        meta = CaptureMetadata(
            client_id="test-client",
            timestamp_mono_ms=int(time.monotonic() * 1000) + i * 100,
            timestamp_wall=_wall(),
        )
        ingest.ingest_chunk(session, pcm, meta)

    await logger.stop()

    # ---------- structural assertions ----------

    # harness_init + session_open + N chunks
    assert len(collected_events) == 2 + N

    raw_audio_events = [e for e in collected_events if e.payload_kind == "raw_audio"]
    assert len(raw_audio_events) == N, "expected exactly N raw_audio events"

    # All raw_audio events must have event_type="raw_audio_chunk"
    for e in raw_audio_events:
        assert e.event_type == "raw_audio_chunk"

    # payload_hash must match the stored blob bytes
    for e in raw_audio_events:
        assert e.payload_ref is not None
        event_id = e.payload_ref.removeprefix("blob://")
        stored = ingest.read_blob(event_id)
        assert hashlib.sha256(stored).hexdigest() == e.payload_hash, (
            f"hash mismatch for {e.event_id}"
        )

    # No orphan events — causal DAG must close
    graph = CausalGraph(collected_events)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"orphan events: {report.orphan_event_ids}, dangling: {report.dangling_refs}"
    )

    # Verify causal chain shape: harness_init has caused_by=[]
    harness_init = next(e for e in collected_events if e.event_type == "harness_init")
    assert harness_init.caused_by == []

    # session_open traces to harness_init
    session_open = next(e for e in collected_events if e.event_type == "session_open")
    assert session_open.caused_by == [harness_init.event_id]

    # first chunk traces to session_open
    first_chunk = raw_audio_events[0]
    assert first_chunk.caused_by == [session_open.event_id]

    # subsequent chunks trace to prior chunk (stream continuity)
    for prev, cur in zip(raw_audio_events, raw_audio_events[1:]):
        assert cur.caused_by == [prev.event_id], (
            f"chunk {cur.seq_no} should trace to {prev.event_id}"
        )

    # seq_no is monotonically increasing across all events
    seq_nos = [e.seq_no for e in collected_events]
    assert seq_nos == sorted(seq_nos), "seq_nos not monotonically increasing"
    assert len(set(seq_nos)) == len(seq_nos), "duplicate seq_nos"

    # sensitivity and retention_policy_id on raw_audio events
    for e in raw_audio_events:
        assert e.sensitivity == "sensitive"
        assert e.retention_policy_id == "raw_media_default_300s"
