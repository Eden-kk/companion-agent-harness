"""T5: /healthz GPU memory budget keys."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from manual_test_console.server import build_app


@pytest.mark.asyncio
async def test_healthz_gpu_keys_present(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        data = await resp.json()
    for key in ("gpu_memory_allocated_mb", "gpu_memory_reserved_mb",
                "gpu_memory_total_mb", "gpu_device_name"):
        assert key in data, f"{key!r} missing from /healthz"


@pytest.mark.asyncio
async def test_healthz_gpu_none_when_cuda_unavailable(tmp_path, monkeypatch):
    fake_torch = types.ModuleType("torch")
    fake_cuda = types.ModuleType("torch.cuda")
    fake_cuda.is_available = lambda: False
    fake_torch.cuda = fake_cuda
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "torch.cuda", fake_cuda)

    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        data = await resp.json()

    assert data["gpu_memory_allocated_mb"] is None
    assert data["gpu_memory_reserved_mb"] is None
    assert data["gpu_memory_total_mb"] is None
    assert data["gpu_device_name"] is None


@pytest.mark.asyncio
async def test_healthz_gpu_keys_int_or_none(tmp_path):
    app = build_app(tmp_path, use_stubs=True)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        data = await resp.json()
    for key in ("gpu_memory_allocated_mb", "gpu_memory_reserved_mb", "gpu_memory_total_mb"):
        val = data[key]
        assert val is None or isinstance(val, int), f"{key} should be int|None, got {type(val)}"
    assert data["gpu_device_name"] is None or isinstance(data["gpu_device_name"], str)
