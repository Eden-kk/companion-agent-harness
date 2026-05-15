"""Contract tests for the manual-test console live-pipeline wiring (Phase 3).

The server wires StreamingRealtimeOrchestrator behind the /ws/ingest endpoint
so a stream of PCM16 chunks produces VAD frames, VAD turn signals, and
policy_decision events on /ws/display in addition to raw_audio_chunk events.

These tests use a fake StreamingDuplexModel (zero proposals) — they MUST NOT
import torch / transformers / MiniCPM. The real model is loaded only at server
startup on b200 via _load_minicpm_streaming_model().
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import AsyncGenerator

import pytest
from aiohttp import ClientSession, WSMsgType
from aiohttp.test_utils import TestServer

from companion_harness.causal_graph import CausalGraph
from companion_harness.schemas import Event, MemoryItem, ThinkerProposal
from manual_test_console.server import build_app


class _FakeStreamingModel:
    """StreamingDuplexModel fake — never yields a proposal.

    Mirrors the pattern used in tests/test_realtime_orchestrator_streaming.py.
    Zero proposals means the orchestrator's grace window expires and
    `synthesis_skipped_no_proposal` is emitted — but `policy_decision` events
    DO get logged, which is what this test cares about.
    """

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        return None

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            return
            yield  # pragma: no cover — makes _gen a generator

        return _gen()


def _pcm_speech_chunk(seed: int, n_samples: int = 1600) -> bytes:
    """PCM16 frame with non-trivial energy (RMS well above EnergyVADModel floor).

    1600 samples = 100 ms @ 16 kHz. Amplitude ±8000 yields RMS ~8000, far above
    the model's rms_ceiling=3000 → p_speech saturates at 1.0.
    """
    out = bytearray()
    for i in range(n_samples):
        v = 8000 if ((seed + i) & 1) else -8000
        out.append(v & 0xFF)
        out.append((v >> 8) & 0xFF)
    return bytes(out)


def _pcm_silent_chunk(n_samples: int = 1600) -> bytes:
    return b"\x00\x00" * n_samples


@pytest.mark.asyncio
async def test_live_pipeline_emits_vad_and_policy_events(tmp_path: Path) -> None:
    """Scripted PCM16 stream produces vad_frame, vad_turn_signal, and
    policy_decision events on the display WS. Causal graph closes."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeStreamingModel(),
    )
    server = TestServer(app)
    await server.start_server()
    try:
        ws_base = f"ws://{server.host}:{server.port}"

        async with ClientSession() as session:
            # Display WS first so it receives session_open + downstream events.
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

            # 8 speech frames + 12 silent frames (~12 * 100 ms = 1.2 s of silence)
            # should drive EnergyVADModel above threshold, then below threshold for
            # well over the 300 ms silence_onset_ms required to fire vad_turn_signal.
            speech_n = 8
            silence_n = 12
            mono_base = 1000
            for i in range(speech_n):
                envelope = {
                    "event_type": "raw_audio",
                    "payload_inline_or_ref": base64.b64encode(_pcm_speech_chunk(i)).decode(),
                    "timestamp_mono_ms": mono_base + i * 100,
                    "client_id": "test-client",
                    "device_label": "test-mic",
                }
                await ingest_ws.send_str(json.dumps(envelope))
            for j in range(silence_n):
                envelope = {
                    "event_type": "raw_audio",
                    "payload_inline_or_ref": base64.b64encode(_pcm_silent_chunk()).decode(),
                    "timestamp_mono_ms": mono_base + (speech_n + j) * 100,
                    "client_id": "test-client",
                    "device_label": "test-mic",
                }
                await ingest_ws.send_str(json.dumps(envelope))

            # Wait until the downstream pipeline has produced at least one
            # policy_decision (or the timeout trips).
            async def wait_for_policy_decision() -> None:
                while True:
                    has_policy = any(e["event_type"] == "policy_decision" for e in received_events)
                    if has_policy:
                        return
                    await asyncio.sleep(0.05)

            await asyncio.wait_for(wait_for_policy_decision(), timeout=10.0)

            await ingest_ws.close()
            await display_ws.close()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
    finally:
        await server.close()

    # ---- Assertions ----
    types = {e["event_type"] for e in received_events}
    assert "raw_audio_chunk" in types
    assert "vad_frame" in types, f"vad_frame missing; saw: {sorted(types)}"
    assert "vad_turn_signal" in types, f"vad_turn_signal missing; saw: {sorted(types)}"
    assert "policy_decision" in types, f"policy_decision missing; saw: {sorted(types)}"

    # Causal graph must close (zero orphans across the full event stream).
    events = [Event(**e) for e in received_events]
    graph = CausalGraph(events)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"orphans: {report.orphan_event_ids}, dangling: {report.dangling_refs}"
    )


@pytest.mark.asyncio
async def test_health_endpoint_reports_live_pipeline_fields(tmp_path: Path) -> None:
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeStreamingModel(),
    )
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/healthz")
            assert resp.status == 200
            body = await resp.json()
            assert body["live_pipeline_enabled"] is True
            assert body["minicpm_loaded"] is True  # fake model counts as "loaded"
            assert body["active_sessions"] == 0
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_capture_only_mode_skips_live_pipeline(tmp_path: Path) -> None:
    """With live_pipeline_enabled=False, ingest still works but no VAD events fire."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        foreground_model=None,
    )
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

            for i in range(3):
                envelope = {
                    "event_type": "raw_audio",
                    "payload_inline_or_ref": base64.b64encode(_pcm_speech_chunk(i)).decode(),
                    "timestamp_mono_ms": 1000 + i * 100,
                    "client_id": "test-client",
                    "device_label": "test-mic",
                }
                await ingest_ws.send_str(json.dumps(envelope))

            async def wait_for_chunks() -> None:
                while True:
                    n = sum(1 for e in received_events if e["event_type"] == "raw_audio_chunk")
                    if n >= 3:
                        return
                    await asyncio.sleep(0.02)

            await asyncio.wait_for(wait_for_chunks(), timeout=5.0)

            await ingest_ws.close()
            await display_ws.close()
            reader_task.cancel()
            try:
                await reader_task
            except asyncio.CancelledError:
                pass
    finally:
        await server.close()

    types = {e["event_type"] for e in received_events}
    assert "raw_audio_chunk" in types
    assert "vad_frame" not in types, "vad_frame fired in capture-only mode"
    assert "policy_decision" not in types, "policy_decision fired in capture-only mode"
