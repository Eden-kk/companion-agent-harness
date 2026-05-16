"""Finding 11 — session leak on browser WS close.

Verifies that when /ws/ingest closes (browser reload / navigation):
  - active_sessions reported by /healthz decrements back to 0
  - any pipeline entry is removed from active_pipelines dict

Uses aiohttp TestServer + a fake foreground model so no GPU/torch needed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession, WSMsgType
from aiohttp.test_utils import TestServer

from manual_test_console.server import KEY_ACTIVE_PIPELINES, build_app


# ---------------------------------------------------------------------------
# Fake foreground model — avoids all GPU/torch imports
# ---------------------------------------------------------------------------


class _FakeDuplexModel:
    """Minimal stand-in accepted by build_live_pipeline as foreground_duplex_model."""

    async def stream_response(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401
        # Never yields — the pipeline runs but produces nothing.
        if False:
            yield  # make it an async generator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _audio_envelope(seed: int = 0, ts_ms: int = 0) -> str:
    # 3200 bytes = 100 ms PCM16 mono @ 16 kHz
    pcm = bytes(((seed + i) & 0xFF) for i in range(3200))
    return json.dumps({
        "event_type": "raw_audio",
        "payload_inline_or_ref": base64.b64encode(pcm).decode(),
        "timestamp_mono_ms": ts_ms,
        "client_id": "test-client",
    })


async def _healthz(session: ClientSession, base: str) -> dict:
    async with session.get(f"{base}/healthz") as resp:
        return await resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_ws_close_decrements_active_sessions(tmp_path: Path) -> None:
    """active_sessions must go from 1 → 0 when the ingest WS closes."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeDuplexModel(),
        use_stubs=True,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        ws_base = f"ws://{server.host}:{server.port}"

        async with ClientSession() as http:
            # Open the ingest WS and push one chunk so the session is live.
            ingest_ws = await http.ws_connect(f"{ws_base}/ws/ingest")
            await ingest_ws.send_str(_audio_envelope(ts_ms=int(time.monotonic() * 1000)))

            # Give the pipeline a moment to register.
            await asyncio.sleep(0.05)

            health_open = await _healthz(http, base)
            assert health_open["active_sessions"] == 1, (
                f"expected 1 active session while WS open, got {health_open['active_sessions']}"
            )

            # Simulate browser navigation: close the WS from the client side.
            await ingest_ws.close()

            # Give the server finalizer a moment to run.
            await asyncio.sleep(0.1)

            health_closed = await _healthz(http, base)
            assert health_closed["active_sessions"] == 0, (
                f"session leak: active_sessions={health_closed['active_sessions']} after WS close"
            )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_ingest_ws_close_removes_pipeline_from_active_pipelines(tmp_path: Path) -> None:
    """active_pipelines dict must be empty after the ingest WS closes."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeDuplexModel(),
        use_stubs=True,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        ws_base = f"ws://{server.host}:{server.port}"

        active_pipelines = server.app[KEY_ACTIVE_PIPELINES]

        async with ClientSession() as http:
            ingest_ws = await http.ws_connect(f"{ws_base}/ws/ingest")
            await ingest_ws.send_str(_audio_envelope(ts_ms=int(time.monotonic() * 1000)))

            await asyncio.sleep(0.05)
            assert len(active_pipelines) == 1, "pipeline should be registered while WS open"

            await ingest_ws.close()
            await asyncio.sleep(0.1)

            assert len(active_pipelines) == 0, (
                f"pipeline not removed: {list(active_pipelines.keys())}"
            )
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_two_sessions_both_cleaned_up(tmp_path: Path) -> None:
    """Opening two ingest WS connections then closing both leaves active_sessions at 0."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeDuplexModel(),
        use_stubs=True,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        ws_base = f"ws://{server.host}:{server.port}"

        async with ClientSession() as http:
            ws1 = await http.ws_connect(f"{ws_base}/ws/ingest")
            ws2 = await http.ws_connect(f"{ws_base}/ws/ingest")

            ts = int(time.monotonic() * 1000)
            await ws1.send_str(_audio_envelope(ts_ms=ts))
            await ws2.send_str(_audio_envelope(ts_ms=ts))
            await asyncio.sleep(0.05)

            h = await _healthz(http, base)
            assert h["active_sessions"] == 2

            await ws1.close()
            await ws2.close()
            await asyncio.sleep(0.1)

            h = await _healthz(http, base)
            assert h["active_sessions"] == 0, (
                f"both sessions should be cleaned up, got active_sessions={h['active_sessions']}"
            )
    finally:
        await server.close()
