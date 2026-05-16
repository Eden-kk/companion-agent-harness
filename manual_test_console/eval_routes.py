"""Eval console route handlers — PR1 (run launcher + artifact browser).

Routes registered:
  GET  /eval                              — serve eval.html
  GET  /eval/static/{name}               — static assets (eval_app.js, eval_style.css)
  GET  /eval/adapters                    — list ADAPTERS registry as JSON
  GET  /eval/runs                        — list completed/active runs from disk
  POST /eval/runs                        — launch a run; returns {run_id, status}
  GET  /eval/runs/{run_id}               — return run manifest from disk
  GET  /eval/runs/{run_id}/event_logs/{case_id} — stream events.jsonl for a case
  POST /eval/runs/{run_id}/cancel        — cancel an active run
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

_STATIC_DIR = Path(__file__).parent

KEY_EVAL_REPORTS_DIR: web.AppKey[Path] = web.AppKey("eval_reports_dir", Path)
KEY_EVAL_RUNS: web.AppKey[dict[str, "EvalRunHandle"]] = web.AppKey("eval_runs", dict)


@dataclass
class EvalRunHandle:
    run_id: str
    adapter: str
    task: asyncio.Task  # type: ignore[type-arg]
    status: str  # "started" | "completed" | "failed" | "cancelled"
    started_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _handle_eval_index(request: web.Request) -> web.Response:
    html_path = _STATIC_DIR / "eval.html"
    return web.FileResponse(html_path)


async def _handle_eval_static(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    # Only serve known eval assets; reject path traversal.
    if name not in ("eval_app.js", "eval_style.css"):
        raise web.HTTPNotFound(reason=f"unknown static asset: {name}")
    return web.FileResponse(_STATIC_DIR / name)


async def _handle_get_adapters(request: web.Request) -> web.Response:
    from companion_harness.evals.registry import ADAPTERS
    result = [
        {
            "name": info.name,
            "version": info.version,
            "status": info.status,
            "case_count": info.case_count,
            "supports_real_mode": info.supports_real_mode,
            "supports_synthetic_mode": info.supports_synthetic_mode,
            "notes": info.notes,
        }
        for info in ADAPTERS.values()
    ]
    return web.json_response(result)


def _load_run_json(run_dir: Path) -> dict[str, Any] | None:
    run_json = run_dir / "run.json"
    if not run_json.exists():
        return None
    try:
        data = json.loads(run_json.read_text())
    except Exception:
        return None
    cases = data.get("cases", [])
    pass_count = sum(1 for c in cases if c.get("final_status") in ("completed", "skipped"))
    return {
        "run_id": data.get("run_id", run_dir.name),
        "adapter": data.get("adapter", "unknown"),
        "started_at": data.get("started_at"),
        "finished_at": data.get("finished_at"),
        "status": data.get("status", "completed"),
        "case_count": len(cases),
        "pass_count": pass_count,
    }


async def _handle_get_runs(request: web.Request) -> web.Response:
    reports_dir: Path = request.app[KEY_EVAL_REPORTS_DIR]
    runs: list[dict] = []
    if reports_dir.exists():
        for run_dir in sorted(reports_dir.iterdir()):
            if run_dir.is_dir():
                summary = _load_run_json(run_dir)
                if summary is not None:
                    # Overlay in-memory status if run is still active.
                    handles: dict[str, EvalRunHandle] = request.app[KEY_EVAL_RUNS]
                    handle = handles.get(summary["run_id"])
                    if handle is not None:
                        summary["status"] = handle.status
                        summary["started_at"] = handle.started_at
                    runs.append(summary)
    return web.json_response(runs)


async def _handle_post_runs(request: web.Request) -> web.Response:
    from companion_harness.evals.registry import ADAPTERS
    from companion_harness.evals.runners import _run_adapter

    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid JSON body")

    adapter_name: str = body.get("adapter", "")
    split: str = body.get("split", "test")

    info = ADAPTERS.get(adapter_name)
    if info is None:
        return web.json_response(
            {
                "error": f"adapter '{adapter_name}' is not registered. "
                         f"Known: {sorted(ADAPTERS)}. "
                         "File new-adapter requests via `gh issue create --label eval-adapter`.",
            },
            status=400,
        )
    if info.status == "disabled":
        return web.json_response(
            {"error": f"adapter '{adapter_name}' is disabled: {info.notes or ''}"},
            status=400,
        )

    reports_dir: Path = request.app[KEY_EVAL_REPORTS_DIR]
    run_id = f"{adapter_name[:8]}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    run_output_dir = str(reports_dir)
    started_at = _now_iso()

    loop = asyncio.get_running_loop()

    def _sync_run() -> int:
        return _run_adapter(info, run_output_dir, split)

    async def _async_run() -> int:
        return await loop.run_in_executor(None, _sync_run)

    task = loop.create_task(_async_run())

    handle = EvalRunHandle(
        run_id=run_id,
        adapter=adapter_name,
        task=task,
        status="started",
        started_at=started_at,
    )
    request.app[KEY_EVAL_RUNS][run_id] = handle

    def _on_done(fut: asyncio.Future) -> None:  # type: ignore[type-arg]
        if fut.cancelled():
            handle.status = "cancelled"
        elif fut.exception():
            handle.status = "failed"
        else:
            handle.status = "completed"

    task.add_done_callback(_on_done)

    return web.json_response({"run_id": run_id, "status": "started"})


async def _handle_get_run(request: web.Request) -> web.Response:
    run_id = request.match_info["run_id"]
    reports_dir: Path = request.app[KEY_EVAL_REPORTS_DIR]
    handles: dict[str, EvalRunHandle] = request.app[KEY_EVAL_RUNS]

    run_json_path = reports_dir / run_id / "run.json"
    if run_json_path.exists():
        data = json.loads(run_json_path.read_text())
        handle = handles.get(run_id)
        if handle is not None:
            data["status"] = handle.status
            data["started_at"] = handle.started_at
        return web.json_response(data)

    handle = handles.get(run_id)
    if handle is not None:
        return web.json_response({
            "run_id": handle.run_id,
            "adapter": handle.adapter,
            "status": handle.status,
            "started_at": handle.started_at,
        })

    raise web.HTTPNotFound(reason=f"run '{run_id}' not found")


async def _handle_get_event_log(request: web.Request) -> web.Response:
    run_id = request.match_info["run_id"]
    case_id = request.match_info["case_id"]
    reports_dir: Path = request.app[KEY_EVAL_REPORTS_DIR]
    jsonl_path = reports_dir / run_id / case_id / "events.jsonl"
    if not jsonl_path.exists():
        return web.json_response({"error": f"event log not found for {run_id}/{case_id}"}, status=404)
    return web.FileResponse(jsonl_path)


async def _handle_post_cancel(request: web.Request) -> web.Response:
    run_id = request.match_info["run_id"]
    handles: dict[str, EvalRunHandle] = request.app[KEY_EVAL_RUNS]
    handle = handles.get(run_id)
    if handle is None:
        raise web.HTTPNotFound(reason=f"run '{run_id}' not found or already finished")
    handle.task.cancel()
    handle.status = "cancelled"
    return web.json_response({"run_id": run_id, "status": "cancelled"})


def register_eval_routes(app: web.Application, *, eval_reports_dir: Path) -> None:
    """Register all /eval/* routes and seed AppKeys."""
    app[KEY_EVAL_REPORTS_DIR] = eval_reports_dir
    app[KEY_EVAL_RUNS] = {}

    app.router.add_get("/eval", _handle_eval_index)
    app.router.add_get("/eval/static/{name}", _handle_eval_static)
    app.router.add_get("/eval/adapters", _handle_get_adapters)
    app.router.add_get("/eval/runs", _handle_get_runs)
    app.router.add_post("/eval/runs", _handle_post_runs)
    app.router.add_get("/eval/runs/{run_id}", _handle_get_run)
    app.router.add_get("/eval/runs/{run_id}/event_logs/{case_id}", _handle_get_event_log)
    app.router.add_post("/eval/runs/{run_id}/cancel", _handle_post_cancel)
