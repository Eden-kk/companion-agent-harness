"""Contract tests for the /ws/audio_out endpoint (live-loop Task 4).

Wires:
  AudioOutputController.play(scripted_chunks)
    → WebSocketAudioSink.__call__(chunk)
    → AudioOutBroker.publish(session_id, seq, chunk)
    → /ws/audio_out listener queue
    → browser-side decode (here: aiohttp WS client)

These tests use a fake TTS (no Kokoro, no torch, no GPU) and assert:
  1. chunks arrive in order, all bytes preserved (base64 round-trip)
  2. seq numbers are monotonic
  3. session_id matches
  4. no chunk fires without an upstream SpeakDecision in the causal graph
     (invariants #2, #4) — i.e. every emitted assistant_audio_buffer_*
     event chains back to a policy_decision via caused_by.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession, WSMsgType
from aiohttp.test_utils import TestServer

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event
from manual_test_console.live_pipeline import WebSocketAudioSink
from manual_test_console.server import AudioOutBroker, build_app


# ---------------------------------------------------------------------------
# Unit-level: WebSocketAudioSink → AudioOutBroker → listener queue
# ---------------------------------------------------------------------------


class _RecordingBroker:
    """Stand-in for AudioOutBroker that captures every published chunk."""

    def __init__(self) -> None:
        self.published: list[tuple[str, int, bytes]] = []

    def publish(self, session_id: str, seq: int, chunk: bytes) -> None:
        self.published.append((session_id, seq, chunk))


@pytest.mark.asyncio
async def test_websocket_audio_sink_publishes_in_order() -> None:
    """WebSocketAudioSink forwards each chunk to the broker with monotonic seq."""
    broker = _RecordingBroker()
    sink = WebSocketAudioSink(session_id="sess-A", broker=broker)

    chunks = [b"chunk1", b"chunk2", b"chunk3"]
    for c in chunks:
        await sink(c)

    assert broker.published == [
        ("sess-A", 1, b"chunk1"),
        ("sess-A", 2, b"chunk2"),
        ("sess-A", 3, b"chunk3"),
    ]


@pytest.mark.asyncio
async def test_audio_output_controller_with_websocket_sink_round_trip() -> None:
    """Drive AudioOutputController.play() with a fake TTS gen → sink → broker.

    Asserts:
      - every scripted chunk reaches the broker (order + bytes preserved)
      - assistant_audio_buffer_flushed event is emitted with caused_by chain
        rooted at the policy decision (invariants #2, #4).
    """
    received: list[Event] = []

    async def event_sink(event: Event) -> None:
        received.append(event)

    logger = EventLogger(event_sink, maxsize=256)
    await logger.start()

    broker = _RecordingBroker()
    sink = WebSocketAudioSink(session_id="sess-B", broker=broker)
    controller = AudioOutputController(
        session_id="sess-B", logger=logger, sink=sink,
    )

    # Synthetic causal predecessor: SpeakDecision approves speech.
    policy_decision_id = "policy-decision-001"
    gen_id = controller.start_generation(caused_by=[policy_decision_id])

    async def fake_tts() -> AsyncIterator[bytes]:
        for c in [b"hello-", b"world-", b"!"]:
            yield c

    await controller.play(fake_tts(), generation_event_id=gen_id)
    await logger.stop()

    # 1. Broker received all chunks in order.
    assert broker.published == [
        ("sess-B", 1, b"hello-"),
        ("sess-B", 2, b"world-"),
        ("sess-B", 3, b"!"),
    ]

    # 2. Causal chain: every assistant_audio_* event traces to policy_decision_id.
    event_types = [e.event_type for e in received]
    assert "assistant_generation_start" in event_types
    assert "assistant_audio_buffer_flushed" in event_types

    gen_start = next(e for e in received if e.event_type == "assistant_generation_start")
    assert policy_decision_id in gen_start.caused_by, (
        "assistant_generation_start did not chain back to policy decision — "
        "violates invariants #2 / #4."
    )
    flushed = next(e for e in received if e.event_type == "assistant_audio_buffer_flushed")
    assert gen_id in flushed.caused_by


# ---------------------------------------------------------------------------
# Integration: /ws/audio_out endpoint receives JSON envelopes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_out_ws_delivers_chunks_in_order(tmp_path: Path) -> None:
    """End-to-end: build_app() → AudioOutBroker → /ws/audio_out client receives
    JSON envelopes with base64-encoded chunks in publish order.
    """
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,  # we do not need a real pipeline here
        foreground_model=None,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        ws_url = f"ws://{server.host}:{server.port}/ws/audio_out"
        async with ClientSession() as session:
            ws = await session.ws_connect(ws_url)

            received: list[dict] = []

            async def reader() -> None:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        received.append(json.loads(msg.data))
                    elif msg.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                        return

            reader_task = asyncio.create_task(reader())

            # Give the writer task a tick to register its queue with the broker
            # before we publish, so the loopback ordering is deterministic.
            await asyncio.sleep(0.05)

            broker: AudioOutBroker = app[
                __import__(
                    "manual_test_console.server", fromlist=["KEY_AUDIO_OUT_BROKER"]
                ).KEY_AUDIO_OUT_BROKER
            ]
            for i, chunk in enumerate([b"\x00\x01", b"\x02\x03", b"\x04\x05"], start=1):
                broker.publish("sess-ws-test", i, chunk)

            async def wait_for_n(n: int) -> None:
                while len(received) < n:
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_n(3), timeout=3.0)

            await ws.close()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
    finally:
        await server.close()

    assert len(received) == 3
    for i, msg in enumerate(received, start=1):
        assert msg["type"] == "audio_chunk"
        assert msg["session_id"] == "sess-ws-test"
        assert msg["seq"] == i
        assert msg["sample_format"] == "pcm_s16le"
        assert msg["sample_rate"] == 24000
    decoded = [base64.b64decode(m["pcm_bytes_b64"]) for m in received]
    assert decoded == [b"\x00\x01", b"\x02\x03", b"\x04\x05"]


@pytest.mark.asyncio
async def test_healthz_reports_audio_out_fields(tmp_path: Path) -> None:
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        foreground_model=None,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/healthz")
            body = await resp.json()
            assert body["audio_out_enabled"] is True
            assert body["audio_out_chunks_sent"] == 0
    finally:
        await server.close()
