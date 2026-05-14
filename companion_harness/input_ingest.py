"""InputIngest adapter — audio-only, Manual-test Task 1.

Wire contract: docs/visionclaw-adaptation-plan-draft.md §4.
Blob store: local filesystem at <blob_dir>/<event_id>, URI = blob://<event_id>.
Causal chain: harness_init (caused_by=[]) -> session_open -> chunk[0] -> chunk[1] -> ...
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

__all__ = ["InputIngest", "CaptureMetadata", "IngestSession"]

_SCHEMA_VERSION = "0.1"
_RETENTION = "raw_media_default_300s"


@dataclass
class CaptureMetadata:
    client_id: str
    timestamp_mono_ms: int
    timestamp_wall: str  # ISO-8601


class IngestSession:
    """Per-connection session state (seq_no counter + causal chain IDs)."""

    def __init__(self, session_id: str, harness_init_id: str, session_open_id: str) -> None:
        self.session_id = session_id
        self.harness_init_id = harness_init_id
        self.session_open_id = session_open_id
        self._seq = 2  # 0=harness_init, 1=session_open
        self._prev_chunk_id: str | None = None

    def next_seq(self) -> int:
        n = self._seq
        self._seq += 1
        return n


class InputIngest:
    """Accepts raw PCM16/16 kHz audio chunks, writes blob store, emits Events.

    Lifecycle:
      ingest = InputIngest(logger, blob_dir)
      session = await ingest.open_session(client_id)
      await ingest.ingest_chunk(session, pcm_bytes, metadata)
      # repeat for each chunk
    """

    def __init__(self, logger: EventLogger, blob_dir: Path) -> None:
        self._logger = logger
        self._blob_dir = blob_dir
        blob_dir.mkdir(parents=True, exist_ok=True)

    def _now_ms(self) -> int:
        return int(time.monotonic() * 1000)

    def _wall(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _write_blob(self, event_id: str, data: bytes) -> str:
        (self._blob_dir / event_id).write_bytes(data)
        return f"blob://{event_id}"

    def read_blob(self, event_id: str) -> bytes:
        return (self._blob_dir / event_id).read_bytes()

    def open_session(self, client_id: str) -> IngestSession:
        session_id = str(uuid.uuid4())
        harness_init_id = str(uuid.uuid4())
        session_open_id = str(uuid.uuid4())
        now_ms = self._now_ms()
        wall = self._wall()

        harness_init = Event(
            event_id=harness_init_id,
            session_id=session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=0,
            event_type="harness_init",
            timestamp_mono_ms=now_ms,
            timestamp_wall=wall,
            source=f"{client_id}.ingest",
            caused_by=[],
            payload_hash="",
            payload_ref=None,
            payload_kind="signal",
            subject_class="unknown",
            sensitivity="safe",
            retention_policy_id="default",
        )
        self._logger.log(harness_init)

        session_open = Event(
            event_id=session_open_id,
            session_id=session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=1,
            event_type="session_open",
            timestamp_mono_ms=now_ms,
            timestamp_wall=wall,
            source=f"{client_id}.ingest",
            caused_by=[harness_init_id],
            payload_hash="",
            payload_ref=None,
            payload_kind="signal",
            subject_class="unknown",
            sensitivity="safe",
            retention_policy_id="default",
        )
        self._logger.log(session_open)

        return IngestSession(session_id, harness_init_id, session_open_id)

    def ingest_chunk(self, session: IngestSession, pcm_bytes: bytes, meta: CaptureMetadata) -> Event:
        event_id = str(uuid.uuid4())
        payload_hash = hashlib.sha256(pcm_bytes).hexdigest()
        payload_ref = self._write_blob(event_id, pcm_bytes)

        caused_by = (
            [session._prev_chunk_id]
            if session._prev_chunk_id is not None
            else [session.session_open_id]
        )

        event = Event(
            event_id=event_id,
            session_id=session.session_id,
            schema_version=_SCHEMA_VERSION,
            seq_no=session.next_seq(),
            event_type="raw_audio_chunk",
            timestamp_mono_ms=meta.timestamp_mono_ms,
            timestamp_wall=meta.timestamp_wall,
            source=f"{meta.client_id}.audio",
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=payload_ref,
            payload_kind="raw_audio",
            subject_class="self",
            sensitivity="sensitive",
            retention_policy_id=_RETENTION,
        )
        session._prev_chunk_id = event_id
        self._logger.log(event)
        return event


# ---------------------------------------------------------------------------
# asyncio WebSocket ingest endpoint
# ---------------------------------------------------------------------------

async def _handle_ws_client(
    websocket: object,
    ingest: InputIngest,
) -> None:
    """Handle one WebSocket connection.

    The receive loop calls EventLogger.log() (non-blocking) and continues
    immediately — it never awaits logger queue draining (invariant #10).
    """
    # Import here so the module is importable without websockets installed
    import websockets  # type: ignore[import-untyped]

    client_id = f"ws-{uuid.uuid4().hex[:8]}"
    session = ingest.open_session(client_id)

    try:
        async for raw in websocket:
            envelope = json.loads(raw)
            pcm_bytes = bytes.fromhex(envelope["data"])
            meta = CaptureMetadata(
                client_id=envelope.get("client_id", client_id),
                timestamp_mono_ms=int(envelope["timestamp_mono_ms"]),
                timestamp_wall=envelope.get(
                    "timestamp_wall",
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            # Non-blocking: log() enqueues without awaiting drain
            ingest.ingest_chunk(session, pcm_bytes, meta)
    except websockets.exceptions.ConnectionClosed:
        pass


async def run_ws_server(
    ingest: InputIngest,
    host: str = "localhost",
    port: int = 8800,
) -> None:
    """Start the WebSocket ingest server.

    The EventLogger drain loop is an independent asyncio task started by the
    caller before invoking this function (invariant #10).
    """
    import websockets  # type: ignore[import-untyped]

    async def handler(ws: object) -> None:
        await _handle_ws_client(ws, ingest)

    async with websockets.serve(handler, host, port):
        await asyncio.Future()  # run forever
