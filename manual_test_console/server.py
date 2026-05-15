"""Manual-test console server — Phase 1 (audio capture + observability).

Single aiohttp process exposes three routes:
  GET  /                — static capture+display page (manual_test_console/index.html)
  GET  /ws/ingest       — WebSocket: browser → harness audio envelopes
  GET  /ws/display      — WebSocket: harness → browser event stream

The ingest WS path constructs a fresh InputIngest session per connection and
calls InputIngest.ingest_chunk() per envelope (non-blocking EventLogger.log
on the realtime path — invariant #10). The display WS path subscribes via
EventLogger.subscribe() with a callback that does an async `put_nowait` onto
a per-connection queue; a writer task per display WS forwards queued events
to JSON over the wire. Backpressure on display drops events for that
viewer only — the realtime ingest path is never blocked.

Run:
  python -m manual_test_console.server --host 0.0.0.0 --port 8800

The page is reachable from the developer's laptop via `ssh -L`:
  ssh -L 8800:localhost:8800 b200
  open http://localhost:8800/ in the browser

Phase 1 scope (per docs/manual-test-module-plan-draft.md §§1, 3-5):
  - audio-only capture
  - observability panel (Events, raw_audio_chunk source/timestamps, caused_by[])
  - NO synthesized voice output (deferred to live-loop integration milestone)
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import dataclasses
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import WSMsgType, web

from companion_harness.event_logger import EventLogger
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.schemas import Event

__all__ = ["build_app", "main"]

# Per-display-WS queue depth. Drops oldest on overflow.
_DISPLAY_QUEUE_DEPTH = 256

# Module path for the static page.
_STATIC_DIR = Path(__file__).parent

# AppKeys (aiohttp 3.9+) — strongly-typed application state keys.
KEY_BROKER: web.AppKey[object] = web.AppKey("broker", object)
KEY_LOGGER: web.AppKey[object] = web.AppKey("logger", object)
KEY_INGEST: web.AppKey[object] = web.AppKey("ingest", object)
KEY_BLOB_DIR: web.AppKey[Path] = web.AppKey("blob_dir", Path)
KEY_CHUNK_COUNTER: web.AppKey[dict] = web.AppKey("chunk_counter", dict)


def _event_to_json(event: Event) -> dict:
    """Serialize an Event to a JSON-safe dict for display."""
    return dataclasses.asdict(event)


def _now_wall() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _null_sink(_event: Event) -> None:
    """Default EventLogger sink — drops events after subscribers have seen them.

    Phase 1 does not persist the event store. Subscribers (display WS) get
    every event live; durability is a Phase-B concern (storage backend).
    """
    return None


# ---------------------------------------------------------------------------
# Display subscription machinery
# ---------------------------------------------------------------------------


class DisplayBroker:
    """Tracks connected display WebSockets and fans events out to each.

    Subscribed to EventLogger via callback. The callback runs on the logger's
    drain task and must not block — it does put_nowait per subscriber and
    drops on QueueFull (per-viewer backpressure isolation; the realtime
    ingest path is never blocked).
    """

    def __init__(self) -> None:
        self._queues: list[asyncio.Queue[dict]] = []
        self._drops_by_queue: dict[int, int] = {}

    def add(self) -> asyncio.Queue[dict]:
        q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_DISPLAY_QUEUE_DEPTH)
        self._queues.append(q)
        self._drops_by_queue[id(q)] = 0
        return q

    def remove(self, q: asyncio.Queue[dict]) -> None:
        if q in self._queues:
            self._queues.remove(q)
        self._drops_by_queue.pop(id(q), None)

    async def on_event(self, event: Event) -> None:
        payload = _event_to_json(event)
        msg = {"kind": "event", "event": payload}
        for q in list(self._queues):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                self._drops_by_queue[id(q)] = self._drops_by_queue.get(id(q), 0) + 1


# ---------------------------------------------------------------------------
# HTTP / WS handlers
# ---------------------------------------------------------------------------


async def _handle_index(request: web.Request) -> web.Response:
    html_path = _STATIC_DIR / "index.html"
    return web.Response(
        body=html_path.read_bytes(),
        content_type="text/html",
        charset="utf-8",
    )


async def _handle_ingest_ws(request: web.Request) -> web.WebSocketResponse:
    """Accept §4 audio envelopes from a single capture page.

    Envelope shape (per dispatch + VisionClaw §4, JSON over WS):
      {
        "event_type": "raw_audio",
        "payload_inline_or_ref": "<base64 PCM16 bytes>",
        "timestamp_mono_ms": <int>,
        "client_id": "<stable id>",
        "device_label": "<mic label>",
        "timestamp_wall": "<ISO-8601, optional>"
      }
    """
    ws = web.WebSocketResponse(heartbeat=30.0, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)

    ingest: InputIngest = request.app[KEY_INGEST]  # type: ignore[assignment]
    chunk_counter: dict[str, int] = request.app[KEY_CHUNK_COUNTER]

    # Fresh session per connection. client_id stamped on first envelope, but
    # session_open must be emitted before any audio — we use a placeholder
    # then update source on each chunk.
    client_id = f"ws-{uuid.uuid4().hex[:8]}"
    session = ingest.open_session(client_id)
    chunk_counter["sessions_opened"] = chunk_counter.get("sessions_opened", 0) + 1

    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                envelope = json.loads(msg.data)
            except json.JSONDecodeError:
                continue

            event_type = envelope.get("event_type")
            if event_type != "raw_audio":
                # Phase 1 ignores other modalities; Phase 2 will add raw_video.
                continue

            payload_b64 = envelope.get("payload_inline_or_ref", "")
            try:
                pcm_bytes = base64.b64decode(payload_b64)
            except (ValueError, TypeError):
                continue

            ts_mono = int(envelope.get("timestamp_mono_ms", 0))
            ts_wall = envelope.get("timestamp_wall") or _now_wall()
            inbound_client_id = envelope.get("client_id") or client_id

            meta = CaptureMetadata(
                client_id=inbound_client_id,
                timestamp_mono_ms=ts_mono,
                timestamp_wall=ts_wall,
            )
            ingest.ingest_chunk(session, pcm_bytes, meta)
            chunk_counter["chunks_ingested"] = chunk_counter.get("chunks_ingested", 0) + 1
    finally:
        await ws.close()
    return ws


async def _handle_display_ws(request: web.Request) -> web.WebSocketResponse:
    """Push every harness Event to one connected viewer."""
    ws = web.WebSocketResponse(heartbeat=30.0)
    await ws.prepare(request)

    broker: DisplayBroker = request.app[KEY_BROKER]  # type: ignore[assignment]
    queue = broker.add()

    async def writer() -> None:
        while True:
            msg = await queue.get()
            try:
                await ws.send_json(msg)
            except ConnectionResetError:
                return

    writer_task = asyncio.create_task(writer())
    try:
        # Read loop: ignore inbound (the page does not send to display WS).
        async for _msg in ws:
            pass
    finally:
        writer_task.cancel()
        broker.remove(queue)
        try:
            await writer_task
        except asyncio.CancelledError:
            pass
        await ws.close()
    return ws


async def _handle_health(request: web.Request) -> web.Response:
    chunk_counter: dict[str, int] = request.app[KEY_CHUNK_COUNTER]
    logger: EventLogger = request.app[KEY_LOGGER]  # type: ignore[assignment]
    return web.json_response({
        "status": "ok",
        "sessions_opened": chunk_counter.get("sessions_opened", 0),
        "chunks_ingested": chunk_counter.get("chunks_ingested", 0),
        "logger_drain_running": logger._task is not None and not logger._task.done(),
    })


# ---------------------------------------------------------------------------
# App factory + lifecycle
# ---------------------------------------------------------------------------


def build_app(blob_dir: Path) -> web.Application:
    """Build the aiohttp Application. Caller is responsible for run/cleanup."""
    app = web.Application()
    broker = DisplayBroker()
    logger = EventLogger(_null_sink)
    logger.subscribe(broker.on_event)
    ingest = InputIngest(logger, blob_dir)

    app[KEY_BROKER] = broker
    app[KEY_LOGGER] = logger
    app[KEY_INGEST] = ingest
    app[KEY_BLOB_DIR] = blob_dir
    app[KEY_CHUNK_COUNTER] = {"sessions_opened": 0, "chunks_ingested": 0}

    app.router.add_get("/", _handle_index)
    app.router.add_get("/healthz", _handle_health)
    app.router.add_get("/ws/ingest", _handle_ingest_ws)
    app.router.add_get("/ws/display", _handle_display_ws)

    async def _on_startup(_app: web.Application) -> None:
        await logger.start()

    async def _on_cleanup(_app: web.Application) -> None:
        await logger.stop()

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument(
        "--blob-dir",
        type=Path,
        default=Path("/tmp/manual_test_blobs"),
        help="Filesystem path where raw audio chunks are written.",
    )
    args = parser.parse_args(argv)

    blob_dir: Path = args.blob_dir
    blob_dir.mkdir(parents=True, exist_ok=True)

    app = build_app(blob_dir)

    print("=" * 72)
    print("manual-test console — Phase 1 (audio capture + observability)")
    print("-" * 72)
    print(f"  bind:        {args.host}:{args.port}")
    print(f"  blob store:  {blob_dir}")
    print(f"  python:      {sys.executable}")
    print(f"  open page:   http://localhost:{args.port}/")
    print(f"  ingest WS:   ws://localhost:{args.port}/ws/ingest")
    print(f"  display WS:  ws://localhost:{args.port}/ws/display")
    print(f"  healthz:     http://localhost:{args.port}/healthz")
    print("=" * 72)
    print("EventLogger drain: starting under aiohttp lifecycle...")
    sys.stdout.flush()

    web.run_app(app, host=args.host, port=args.port, print=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
