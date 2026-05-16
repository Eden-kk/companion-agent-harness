"""N1 fix: /metrics Prometheus endpoint."""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

from manual_test_console.server import build_app


@pytest.mark.asyncio
async def test_metrics_returns_200(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
    assert resp.status == 200


@pytest.mark.asyncio
async def test_metrics_content_type(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
    assert "text/plain" in resp.content_type
    assert "version=0.0.4" in resp.headers["Content-Type"]


@pytest.mark.asyncio
async def test_metrics_required_metric_names(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
        body = await resp.text()
    assert "harness_uptime_seconds" in body
    assert "harness_events_per_second_last_60s" in body
    assert "harness_adapter_ready" in body


@pytest.mark.asyncio
async def test_metrics_adapter_labels_present(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
        body = await resp.text()
    for adapter in ("vad", "asr", "tts"):
        assert f'adapter="{adapter}"' in body, f"missing adapter label: {adapter}"


@pytest.mark.asyncio
async def test_metrics_prometheus_syntax(tmp_path):
    """Every non-empty, non-comment line must be a valid Prometheus sample line."""
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/metrics")
        body = await resp.text()
    for line in body.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        assert len(parts) >= 2, f"unparseable sample line: {line!r}"
        float(parts[-1])  # value must be numeric
