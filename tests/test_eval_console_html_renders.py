"""Tests for eval HTML serving (E5+E6).

Success criterion:
  GET /eval → 200, text/html, contains id="adapter-panel" and failure inspector placeholder.
  GET / → body contains href="/eval" (switcher present).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from manual_test_console.server import build_app


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


@pytest.mark.asyncio
async def test_eval_html_serves_at_eval_route(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/eval")
            assert resp.status == 200
            ct = resp.headers.get("Content-Type", "")
            assert "text/html" in ct
            body = await resp.text()
            assert 'id="adapter-panel"' in body
            assert "Failure inspector" in body
            assert "CausalFailureSliceExtractor" in body
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_index_html_contains_eval_switcher(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/")
            assert resp.status == 200
            body = await resp.text()
            assert 'href="/eval"' in body
    finally:
        await server.close()
