"""N6 regression: /eval/runs/{id}/event_logs/{case_id} reader must match writer path.

Writer (harness_native.py) produces:  <reports>/<run_id>/event_logs/<case_id>.jsonl
Reader (eval_routes.py) must consume: <reports>/<run_id>/event_logs/<case_id>.jsonl
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from manual_test_console.server import build_app


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


@pytest.mark.asyncio
async def test_event_log_endpoint_matches_writer_path(tmp_path: Path) -> None:
    run_id = "run-abc123"
    case_id = "thinking_pause"
    blobs = tmp_path / "blobs"
    # Mirror exactly what harness_native.py writes
    event_log_dir = blobs / "eval_reports" / run_id / "event_logs"
    event_log_dir.mkdir(parents=True)
    content = b'{"kind":"vad_frame"}\n{"kind":"policy_decision"}\n'
    (event_log_dir / f"{case_id}.jsonl").write_bytes(content)

    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get(f"/eval/runs/{run_id}/event_logs/{case_id}")
            assert resp.status == 200
            body = await resp.read()
            assert body == content
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_event_log_endpoint_404_for_unknown_case(tmp_path: Path) -> None:
    run_id = "run-abc123"
    blobs = tmp_path / "blobs"
    (blobs / "eval_reports" / run_id / "event_logs").mkdir(parents=True)

    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get(f"/eval/runs/{run_id}/event_logs/nonexistent_case")
            assert resp.status == 404
    finally:
        await server.close()
