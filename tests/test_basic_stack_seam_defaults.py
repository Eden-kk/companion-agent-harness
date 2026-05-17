"""Contract tests: basic-stack seam defaults at fresh console startup."""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestServer
from aiohttp import ClientSession

from manual_test_console.config_schema import HOT_SEAMS
from manual_test_console.server import KEY_CONFIG_STORE, _BASIC_STACK_SEAM_DEFAULTS, build_app


_EXPECTED_ON = {"vad", "asr", "tts"}
_EXPECTED_OFF = set(HOT_SEAMS) - _EXPECTED_ON


def _make_app(tmp_path: Path, **kwargs):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False, **kwargs)


@pytest.mark.asyncio
async def test_fresh_app_boots_with_vad_asr_tts_only(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with ClientSession() as session:
            resp = await session.get(f"http://{server.host}:{server.port}/config/seams")
            assert resp.status == 200
            body = await resp.json()
        seam_map = {s["seam"]: s["enabled"] for s in body["seams"]}
        for seam in _EXPECTED_ON:
            assert seam_map[seam] is True, f"{seam} should be enabled"
        for seam in _EXPECTED_OFF:
            assert seam_map[seam] is False, f"{seam} should be disabled"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_disable_seam_cli_flag_overrides_default(tmp_path: Path) -> None:
    seam_defaults = dict(_BASIC_STACK_SEAM_DEFAULTS)
    seam_defaults["asr"] = False
    app = _make_app(tmp_path, seam_defaults=seam_defaults)
    server = TestServer(app)
    await server.start_server()
    try:
        async with ClientSession() as session:
            resp = await session.get(f"http://{server.host}:{server.port}/config/seams")
            body = await resp.json()
        seam_map = {s["seam"]: s["enabled"] for s in body["seams"]}
        assert seam_map["asr"] is False
        assert seam_map["vad"] is True
        assert seam_map["tts"] is True
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_dashboard_toggle_still_works_at_runtime(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "smart_turn", "enabled": True},
            )
            assert resp.status == 200
            resp2 = await session.get(f"{base}/config/seams")
            body = await resp2.json()
        seam_map = {s["seam"]: s["enabled"] for s in body["seams"]}
        assert seam_map["smart_turn"] is True
        assert seam_map["vad"] is True
    finally:
        await server.close()


def test_basic_stack_defaults_constant_matches_handbook() -> None:
    assert set(_BASIC_STACK_SEAM_DEFAULTS.keys()) == set(HOT_SEAMS)
    assert len(_BASIC_STACK_SEAM_DEFAULTS) == 12
    for seam in _EXPECTED_ON:
        assert _BASIC_STACK_SEAM_DEFAULTS[seam] is True, f"{seam} must be on"
    for seam in _EXPECTED_OFF:
        assert _BASIC_STACK_SEAM_DEFAULTS[seam] is False, f"{seam} must be off"
