"""Contract tests for the seam-state HTTP routes (D1).

Routes under test:
  GET  /config/seams      — list all 12 hot seams with enabled state
  POST /config/model-swap — toggle a seam enabled/disabled
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from manual_test_console.config_schema import HOT_SEAMS
from manual_test_console.server import KEY_CONFIG_STORE, build_app


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


@pytest.mark.asyncio
async def test_get_config_seams_returns_basic_stack_defaults(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/config/seams")
            assert resp.status == 200
            body = await resp.json()

        seams = body["seams"]
        assert len(seams) == 12
        names = [s["seam"] for s in seams]
        assert names == list(HOT_SEAMS)
        seam_map = {s["seam"]: s["enabled"] for s in seams}
        assert seam_map["vad"] is True
        assert seam_map["asr"] is True
        assert seam_map["tts"] is True
        for off_seam in ("smart_turn", "backchannel", "scene_scorer", "grounding_model",
                         "av_conflict_scorer", "urgency_scorer", "embedder",
                         "attachment_risk_monitor", "fast_tool_dispatcher"):
            assert seam_map[off_seam] is False, f"{off_seam} should be disabled by default"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_model_swap_valid_body_returns_accepted(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "vad", "enabled": False},
            )
            assert resp.status == 200
            body = await resp.json()

        assert body["accepted"] is True
        assert "model_swap_event_id" in body
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_model_swap_mutates_config_store(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            await session.post(
                f"{base}/config/model-swap",
                json={"seam": "tts", "enabled": False},
            )
            resp = await session.get(f"{base}/config/seams")
            body = await resp.json()

        seam_map = {s["seam"]: s["enabled"] for s in body["seams"]}
        assert seam_map["tts"] is False
        # others remain enabled
        assert seam_map["vad"] is True
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_model_swap_unknown_seam_returns_403(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "bogus", "enabled": False},
            )
            assert resp.status == 403
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_model_swap_int_enabled_returns_400(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "vad", "enabled": 1},
            )
            assert resp.status == 400
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_get_config_includes_seams_key(tmp_path: Path) -> None:
    """GET /config top-level response includes a 'seams' key."""
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/config")
            assert resp.status == 200
            body = await resp.json()

        assert "seams" in body
        assert len(body["seams"]) == 12
    finally:
        await server.close()
