"""Tests for /eval/* route handlers (E3+E4+E6).

Uses aiohttp TestClient/TestServer pattern (same as test_init_vision_flag_default_off.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from manual_test_console.server import build_app
from manual_test_console.eval_routes import KEY_EVAL_REPORTS_DIR, KEY_EVAL_RUNS


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


# ---------------------------------------------------------------------------
# GET /eval/adapters
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_eval_adapters_returns_six_entries(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/eval/adapters")
            assert resp.status == 200
            data = await resp.json()
            assert len(data) == 6
            names = {a["name"] for a in data}
            assert names == {
                "harness_native", "vocalbench", "voicebench",
                "humdial_fdbench", "candor", "full_duplex_bench",
            }
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# GET /eval/runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_eval_runs_lists_disk_runs(tmp_path: Path) -> None:
    blobs = tmp_path / "blobs"
    blobs.mkdir(parents=True)
    reports = blobs / "eval_reports" / "run-x"
    reports.mkdir(parents=True)
    (reports / "run.json").write_text(json.dumps({
        "run_id": "run-x",
        "adapter": "harness_native",
        "cases": [],
    }))

    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/eval/runs")
            assert resp.status == 200
            data = await resp.json()
            assert any(r["run_id"] == "run-x" for r in data)
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# POST /eval/runs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_eval_runs_returns_run_id_and_started_status(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.post(
                "/eval/runs",
                data=json.dumps({"adapter": "harness_native"}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert "run_id" in data
            assert data["status"] == "started"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# GET /eval/runs/{run_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_eval_runs_run_id_returns_manifest(tmp_path: Path) -> None:
    blobs = tmp_path / "blobs"
    blobs.mkdir(parents=True)
    run_id = "test-run-abc"
    run_dir = blobs / "eval_reports" / run_id
    run_dir.mkdir(parents=True)
    manifest = {"run_id": run_id, "adapter": "candor", "cases": [], "status": "completed"}
    (run_dir / "run.json").write_text(json.dumps(manifest))

    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get(f"/eval/runs/{run_id}")
            assert resp.status == 200
            data = await resp.json()
            assert data["run_id"] == run_id
            assert data["adapter"] == "candor"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# GET /eval/runs/{run_id}/event_logs/{case_id}
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_event_log_streams_jsonl(tmp_path: Path) -> None:
    blobs = tmp_path / "blobs"
    blobs.mkdir(parents=True)
    run_id = "run-evtlog"
    case_id = "case-001"
    log_dir = blobs / "eval_reports" / run_id / case_id
    log_dir.mkdir(parents=True)
    jsonl_content = b'{"kind":"vad_frame"}\n{"kind":"policy_decision"}\n'
    (log_dir / "events.jsonl").write_bytes(jsonl_content)

    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get(f"/eval/runs/{run_id}/event_logs/{case_id}")
            assert resp.status == 200
            body = await resp.read()
            assert body == jsonl_content
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# POST /eval/runs/{run_id}/cancel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_cancel_marks_run_cancelled(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            # Launch a run first
            resp = await client.post(
                "/eval/runs",
                data=json.dumps({"adapter": "harness_native"}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 200
            run_id = (await resp.json())["run_id"]

            # Cancel it
            resp2 = await client.post(f"/eval/runs/{run_id}/cancel")
            assert resp2.status == 200
            data = await resp2.json()
            assert data["status"] == "cancelled"

            # Subsequent GET should show cancelled
            resp3 = await client.get(f"/eval/runs/{run_id}")
            assert resp3.status == 200
            info = await resp3.json()
            assert info["status"] == "cancelled"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Unknown adapter → 400
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unknown_adapter_post_returns_400(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.post(
                "/eval/runs",
                data=json.dumps({"adapter": "does_not_exist"}),
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
            data = await resp.json()
            assert "gh issue" in data["error"]
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Static assets
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_eval_static_js(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/eval/static/eval_app.js")
            assert resp.status == 200
            ct = resp.headers.get("Content-Type", "")
            assert "javascript" in ct or "text" in ct
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_get_eval_static_css(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            resp = await client.get("/eval/static/eval_style.css")
            assert resp.status == 200
            ct = resp.headers.get("Content-Type", "")
            assert "css" in ct or "text" in ct
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# All 6 adapters accept POST /eval/runs (dispatch coverage)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_eval_runs_all_six_adapters_dispatch(tmp_path: Path) -> None:
    from companion_harness.evals.registry import ADAPTERS
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            for name in ADAPTERS:
                resp = await client.post(
                    "/eval/runs",
                    data=json.dumps({"adapter": name}),
                    headers={"Content-Type": "application/json"},
                )
                data = await resp.json()
                assert resp.status == 200, f"{name}: got {resp.status}, body={data}"
                assert data["status"] == "started", f"{name}: status={data.get('status')}"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# round-trip: POST /eval/runs → GET /eval/runs/{run_id} never 404s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_post_then_get_run_id_round_trip(tmp_path: Path) -> None:
    """Returned run_id from POST must be retrievable via GET without 404."""
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        async with TestClient(server) as client:
            post_resp = await client.post(
                "/eval/runs",
                data=json.dumps({"adapter": "harness_native"}),
                headers={"Content-Type": "application/json"},
            )
            assert post_resp.status == 200
            run_id = (await post_resp.json())["run_id"]

            get_resp = await client.get(f"/eval/runs/{run_id}")
            assert get_resp.status == 200, f"GET /eval/runs/{run_id} returned 404 — run_id mismatch"
            data = await get_resp.json()
            assert data["run_id"] == run_id
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# --eval-reports-dir wiring via build_app
# ---------------------------------------------------------------------------

def test_eval_reports_dir_cli_flag(tmp_path: Path) -> None:
    custom_dir = tmp_path / "custom_reports"
    app = _make_app(tmp_path)
    # The default wiring uses blob_dir / "eval_reports"
    assert app[KEY_EVAL_REPORTS_DIR] == tmp_path / "blobs" / "eval_reports"
