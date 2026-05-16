"""T4: /healthz per-adapter readiness extension."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from manual_test_console.server import build_app


@pytest.mark.asyncio
async def test_healthz_ready_keys_present_with_stubs(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        data = await resp.json()
    for key in ("vad_ready", "smart_turn_ready", "backchannel_ready",
                "asr_ready", "tts_ready", "vision_ready", "foreground_model_ready"):
        assert key in data, f"key {key!r} missing from /healthz"
        assert isinstance(data[key], bool), f"{key} should be bool, got {type(data[key])}"


@pytest.mark.asyncio
async def test_healthz_all_false_when_stubs_and_no_model(tmp_path):
    app = build_app(tmp_path, use_stubs=True, live_pipeline_enabled=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        data = await resp.json()
    assert data["vad_ready"] is False
    assert data["smart_turn_ready"] is False
    assert data["backchannel_ready"] is False
    assert data["asr_ready"] is False
    assert data["foreground_model_ready"] is False


@pytest.mark.asyncio
async def test_healthz_foreground_model_ready_true_when_injected(tmp_path):
    class _FakeModel:
        pass

    app = build_app(tmp_path, foreground_model=_FakeModel(), use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        data = await resp.json()
    assert data["foreground_model_ready"] is True


@pytest.mark.asyncio
async def test_healthz_pre_existing_keys_preserved(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        data = await resp.json()
    pre_existing = (
        "status", "mode", "sessions_opened", "chunks_ingested", "frames_ingested",
        "logger_drain_running", "live_pipeline_enabled", "minicpm_loaded",
        "active_sessions", "use_stubs", "vad_model", "smart_turn_model",
        "backchannel_model", "asr_model", "audio_out_enabled", "tts_model",
        "vision_enabled",
    )
    for key in pre_existing:
        assert key in data, f"pre-existing key {key!r} missing"
