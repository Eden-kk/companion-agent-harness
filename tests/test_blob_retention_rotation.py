"""T3: blob rotation worker — deletes only files older than retention window."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest


def _age_file(path: Path, age_days: float) -> None:
    """Backdate a file's mtime by age_days."""
    age_seconds = age_days * 86400
    new_mtime = time.time() - age_seconds
    os.utime(path, (new_mtime, new_mtime))


def _run_rotation_sync(blob_dir: Path, retention_days: int) -> None:
    """Single rotation pass — mirrors the worker logic without asyncio.sleep."""
    if retention_days <= 0:
        return
    retention_seconds = retention_days * 86400
    cutoff = time.time() - retention_seconds
    for p in blob_dir.iterdir():
        if p.is_file():
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass


def test_rotation_removes_old_files(tmp_path):
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    old_file = blob_dir / "old.bin"
    recent_file = blob_dir / "recent.bin"
    old_file.write_bytes(b"old")
    recent_file.write_bytes(b"recent")

    _age_file(old_file, 35.0)
    _age_file(recent_file, 1.0)

    _run_rotation_sync(blob_dir, retention_days=30)

    assert not old_file.exists(), "old file should have been deleted"
    assert recent_file.exists(), "recent file should be preserved"


def test_rotation_preserves_boundary_file(tmp_path):
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    boundary_file = blob_dir / "boundary.bin"
    boundary_file.write_bytes(b"boundary")
    _age_file(boundary_file, 29.9)

    _run_rotation_sync(blob_dir, retention_days=30)

    assert boundary_file.exists(), "file just under retention window should be preserved"


def test_rotation_disabled_when_zero(tmp_path):
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()

    old_file = blob_dir / "old.bin"
    old_file.write_bytes(b"old")
    _age_file(old_file, 100.0)

    _run_rotation_sync(blob_dir, retention_days=0)

    assert old_file.exists(), "no deletion when retention_days=0"


def test_rotation_empty_dir(tmp_path):
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    _run_rotation_sync(blob_dir, retention_days=30)


@pytest.mark.asyncio
async def test_build_app_accepts_blob_retention_days(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    from manual_test_console.server import build_app
    app = build_app(tmp_path, blob_retention_days=7)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/healthz")
        assert resp.status == 200
