"""Contract tests for manual-test console.

Phase 1 (audio roundtrip):
- A scripted WebSocket client streams N PCM16 chunks to /ws/ingest.
- The server emits N raw_audio Events (plus harness_init + session_open).
- /ws/display pushes the same Events to a connected viewer.
- caused_by[] closes (no orphans) across the full event stream.

Phase 2 (video roundtrip):
- Scripted JPEG frames stream as raw_video envelopes.
- Server emits N raw_video_frame Events with closed caused_by[].
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path

import pytest
from aiohttp import ClientSession, WSMsgType

from companion_harness.causal_graph import CausalGraph
from companion_harness.schemas import Event
from manual_test_console.server import build_app


def _pcm_chunk(seed: int) -> bytes:
    # 3200 bytes = 100ms PCM16 mono @ 16kHz
    return bytes(((seed + i) & 0xFF) for i in range(3200))


@pytest.mark.asyncio
async def test_audio_ingest_to_display_roundtrip(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestServer

    app = build_app(blob_dir=tmp_path / "blobs")
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        ws_base = f"ws://{server.host}:{server.port}"

        async with ClientSession() as session:
            # 1. Connect display WS FIRST so it receives session_open + chunks.
            display_ws = await session.ws_connect(f"{ws_base}/ws/display")

            received_events: list[dict] = []

            async def reader() -> None:
                async for msg in display_ws:
                    if msg.type == WSMsgType.TEXT:
                        parsed = json.loads(msg.data)
                        if parsed.get("kind") == "event":
                            received_events.append(parsed["event"])
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        return

            reader_task = asyncio.create_task(reader())

            # 2. Connect ingest WS and stream N chunks.
            ingest_ws = await session.ws_connect(f"{ws_base}/ws/ingest")

            N = 10
            mono_base = int(time.monotonic() * 1000)
            for i in range(N):
                envelope = {
                    "event_type": "raw_audio",
                    "payload_inline_or_ref": base64.b64encode(_pcm_chunk(i)).decode(),
                    "timestamp_mono_ms": mono_base + i * 100,
                    "client_id": "test-client",
                    "device_label": "test-mic",
                }
                await ingest_ws.send_str(json.dumps(envelope))

            # 3. Wait for the display side to see N raw_audio Events plus
            #    harness_init + session_open. Bound the wait.
            async def wait_for_events() -> None:
                while True:
                    raw_audio = [e for e in received_events if e["payload_kind"] == "raw_audio"]
                    has_init = any(e["event_type"] == "harness_init" for e in received_events)
                    has_open = any(e["event_type"] == "session_open" for e in received_events)
                    if len(raw_audio) >= N and has_init and has_open:
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_events(), timeout=5.0)

            await ingest_ws.close()
            await display_ws.close()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

        # --- assertions ---

        raw_audio = [e for e in received_events if e["payload_kind"] == "raw_audio"]
        assert len(raw_audio) == N, f"expected {N} raw_audio events, got {len(raw_audio)}"
        for e in raw_audio:
            assert e["event_type"] == "raw_audio_chunk"
            assert e["sensitivity"] == "sensitive"
            assert e["retention_policy_id"] == "raw_media_default_300s"
            assert e["payload_ref"] and e["payload_ref"].startswith("blob://")
            assert e["caused_by"], "raw_audio chunk must have non-empty caused_by[]"

        # All events sent through the display WS should form a closed DAG.
        events = [Event(**e) for e in received_events]
        graph = CausalGraph(events)
        report = graph.find_orphans()
        assert report.orphan_count == 0, (
            f"orphans: {report.orphan_event_ids}, dangling: {report.dangling_refs}"
        )

        # First chunk traces to session_open; subsequent chunks chain.
        chunks_in_order = [e for e in received_events if e["payload_kind"] == "raw_audio"]
        chunks_in_order.sort(key=lambda e: e["seq_no"])
        session_open = next(e for e in received_events if e["event_type"] == "session_open")
        assert chunks_in_order[0]["caused_by"] == [session_open["event_id"]]
        for prev, cur in zip(chunks_in_order, chunks_in_order[1:]):
            assert cur["caused_by"] == [prev["event_id"]]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_video_ingest_to_display_roundtrip(tmp_path: Path) -> None:
    """Phase 2: scripted JPEG frames flow as raw_video Events with closed caused_by[]."""
    from aiohttp.test_utils import TestServer

    app = build_app(blob_dir=tmp_path / "blobs")
    server = TestServer(app)
    await server.start_server()
    try:
        ws_base = f"ws://{server.host}:{server.port}"

        async with ClientSession() as session:
            display_ws = await session.ws_connect(f"{ws_base}/ws/display")
            received_events: list[dict] = []

            async def reader() -> None:
                async for msg in display_ws:
                    if msg.type == WSMsgType.TEXT:
                        parsed = json.loads(msg.data)
                        if parsed.get("kind") == "event":
                            received_events.append(parsed["event"])
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        return

            reader_task = asyncio.create_task(reader())
            ingest_ws = await session.ws_connect(f"{ws_base}/ws/ingest")

            N = 4
            # 1-byte-different fake JPEG payloads (no actual decoding happens server-side).
            mono_base = int(time.monotonic() * 1000)
            for i in range(N):
                fake_jpeg = b"\xff\xd8\xff\xe0" + bytes([i]) * 64 + b"\xff\xd9"
                envelope = {
                    "event_type": "raw_video",
                    "payload_inline_or_ref": base64.b64encode(fake_jpeg).decode(),
                    "timestamp_mono_ms": mono_base + i * 1000,
                    "client_id": "test-client",
                    "device_label": "test-camera",
                }
                await ingest_ws.send_str(json.dumps(envelope))

            async def wait_for_frames() -> None:
                while True:
                    raw_video = [e for e in received_events if e["payload_kind"] == "raw_video"]
                    if len(raw_video) >= N:
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_frames(), timeout=5.0)

            await ingest_ws.close()
            await display_ws.close()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass

        raw_video = [e for e in received_events if e["payload_kind"] == "raw_video"]
        assert len(raw_video) == N
        for e in raw_video:
            assert e["event_type"] == "raw_video_frame"
            assert e["sensitivity"] == "sensitive"
            assert e["payload_ref"] and e["payload_ref"].startswith("blob://")

        events = [Event(**e) for e in received_events]
        graph = CausalGraph(events)
        report = graph.find_orphans()
        assert report.orphan_count == 0, (
            f"orphans: {report.orphan_event_ids}, dangling: {report.dangling_refs}"
        )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_health_endpoint_reports_counters(tmp_path: Path) -> None:
    from aiohttp.test_utils import TestServer

    app = build_app(blob_dir=tmp_path / "blobs")
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/healthz")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert "sessions_opened" in body
            assert "chunks_ingested" in body
            assert "frames_ingested" in body
            assert body["logger_drain_running"] is True
    finally:
        await server.close()
