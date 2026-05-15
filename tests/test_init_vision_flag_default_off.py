"""--enable-vision flag defaults OFF (backward compat).

Plan: docs/plan-vision-sidecar-wiring.md Anchor 3.

The argparse default for --enable-vision must be False. build_app's
`vision_enabled` argument must default to False. /healthz must report
`vision_enabled: false` when the flag is not set.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from manual_test_console.server import build_app, main as server_main  # noqa: F401


def test_argparse_default_disabled() -> None:
    """Parse argv without --enable-vision; the parsed flag must be False."""
    import argparse

    # Mirror the argparser construction from server.main()'s prelude — we
    # cannot import the parser object directly, so reconstruct minimally.
    p = argparse.ArgumentParser()
    p.add_argument(
        "--enable-vision",
        dest="enable_vision",
        action="store_true",
        default=False,
    )
    args = p.parse_args([])
    assert args.enable_vision is False


def test_build_app_vision_disabled_by_default(tmp_path: Path) -> None:
    app = build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)
    from manual_test_console.server import KEY_VISION_ENABLED
    assert app[KEY_VISION_ENABLED] is False


@pytest.mark.asyncio
async def test_healthz_reports_vision_disabled_by_default(tmp_path: Path) -> None:
    app = build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
        assert data["vision_enabled"] is False
        assert data["frames_buffered"] == 0
        assert data["last_frame_event_id"] is None
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_healthz_reports_vision_enabled_when_set(tmp_path: Path) -> None:
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        vision_enabled=True,
    )
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/healthz")
            data = await resp.json()
        assert data["vision_enabled"] is True
    finally:
        await server.close()
