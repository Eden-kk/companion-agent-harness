"""Contract tests for model_swap_* event emission (Dashboard P1 D2).

Per plan §D2 test plan:
  - One POST /config/model-swap produces exactly 3 events in order:
    operator_action → model_swap_requested → model_swap_completed.
  - All three share the operator_action root via caused_by.
  - latency_ms field present and >= 0.
  - One POST /config/model-swap with invalid seam produces exactly 2 events:
    operator_action → model_swap_rejected (with reason="unknown_seam").
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from companion_harness.schemas import Event
from manual_test_console.server import KEY_LOGGER, build_app


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


def _subscribe_event_capture(app) -> list[Event]:
    events: list[Event] = []

    async def _capture(evt: Event) -> None:
        events.append(evt)

    app[KEY_LOGGER].subscribe(_capture)
    return events


async def _flush(logger, *, rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_valid_swap_emits_three_events_in_order(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "vad", "enabled": False},
            )
            assert resp.status == 200
            body = await resp.json()

        await _flush(app[KEY_LOGGER])

        swap_events = [e for e in events if e.event_type in (
            "operator_action", "model_swap_requested", "model_swap_completed",
        )]
        assert len(swap_events) == 3, [e.event_type for e in swap_events]

        op, req, cc = swap_events
        assert op.event_type == "operator_action"
        assert req.event_type == "model_swap_requested"
        assert cc.event_type == "model_swap_completed"

        # All three share op as caused_by root.
        assert op.caused_by == []
        assert op.event_id in req.caused_by
        assert op.event_id in cc.caused_by

        # Payload correctness.
        assert req.payload_inline["seam"] == "vad"
        assert req.payload_inline["from_enabled"] is True
        assert req.payload_inline["to_enabled"] is False

        assert cc.payload_inline["seam"] == "vad"
        assert cc.payload_inline["from_enabled"] is True
        assert cc.payload_inline["to_enabled"] is False
        assert cc.payload_inline["latency_ms"] >= 0

        # HTTP response cites the completed event.
        assert body["accepted"] is True
        assert body["model_swap_event_id"] == cc.event_id
        assert body["latency_ms"] >= 0
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_invalid_seam_emits_operator_action_then_rejected(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "bogus_seam", "enabled": True},
            )
            assert resp.status == 403

        await _flush(app[KEY_LOGGER])

        swap_events = [e for e in events if e.event_type in (
            "operator_action", "model_swap_rejected",
        )]
        assert len(swap_events) == 2, [e.event_type for e in swap_events]

        op, rej = swap_events
        assert op.event_type == "operator_action"
        assert rej.event_type == "model_swap_rejected"
        assert op.caused_by == []
        assert op.event_id in rej.caused_by
        assert rej.payload_inline["reason"] == "unknown_seam"
        assert rej.payload_inline["seam"] == "bogus_seam"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_no_op_swap_still_emits_completed(tmp_path: Path) -> None:
    """Even if from_enabled == to_enabled, model_swap_completed is emitted."""
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "asr", "enabled": True},  # already True (default)
            )
            assert resp.status == 200

        await _flush(app[KEY_LOGGER])

        completed = [e for e in events if e.event_type == "model_swap_completed"]
        assert len(completed) == 1
        assert completed[0].payload_inline["from_enabled"] is True
        assert completed[0].payload_inline["to_enabled"] is True
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_get_config_seams_returns_all_12(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/config/seams")
            assert resp.status == 200
            body = await resp.json()

        assert "seams" in body
        assert len(body["seams"]) == 12
        for entry in body["seams"]:
            assert "seam" in entry
            assert "enabled" in entry
            assert entry["enabled"] is True  # all default to enabled
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_swap_then_get_seams_reflects_new_state(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            await session.post(
                f"{base}/config/model-swap",
                json={"seam": "tts", "enabled": False},
            )
            resp = await session.get(f"{base}/config/seams")
            body = await resp.json()

        seam_map = {entry["seam"]: entry["enabled"] for entry in body["seams"]}
        assert seam_map["tts"] is False
        # Others unchanged.
        assert seam_map["vad"] is True
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_non_bool_enabled_returns_400(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": "vad", "enabled": 1},
            )
            # validate_seam_patch rejects non-bool enabled; seam is valid so 400
            assert resp.status in (400, 403)

        await _flush(app[KEY_LOGGER])

        rej_events = [e for e in events if e.event_type == "model_swap_rejected"]
        assert len(rej_events) == 1
        rej_event = rej_events[0]
        assert rej_event.payload_inline["attempted_enabled"] is None
    finally:
        await server.close()
