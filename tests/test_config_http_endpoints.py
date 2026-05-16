"""Contract tests for the /config HTTP endpoints (Phase 1 Task E).

See docs/design-config-and-dashboard.md §5 (server API) and §8 (event log).

The endpoints under test:
  GET  /config           — current values + schema metadata
  POST /config/patch     — apply one Tier-B override
  POST /config/reset     — reset key / section / all

Each successful patch/reset emits one operator_action (root) plus one
config_change per actual change, with the config_change citing the
operator_action via caused_by[]. These tests assert that DAG closure on
the EventLogger event stream.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from companion_harness.schemas import Event
from manual_test_console.config_schema import ALLOWLIST
from manual_test_console.server import KEY_CONFIG_STORE, KEY_LOGGER, build_app


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


def _subscribe_event_capture(app) -> list[Event]:
    """Attach a capture-callback to the EventLogger; return the list it fills."""
    events: list[Event] = []

    async def _capture(evt: Event) -> None:
        events.append(evt)

    app[KEY_LOGGER].subscribe(_capture)
    return events


async def _flush(logger, *, rounds: int = 10) -> None:
    """Yield control so the logger drain task delivers to subscribers."""
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_get_config_returns_values_and_schema(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.get(f"{base}/config")
            assert resp.status == 200
            body = await resp.json()

        assert {"values", "schema"}.issubset(body.keys())
        assert len(body["values"]) == 14  # 12 original + 2 reasoner budget keys (v0.2a T3)
        assert len(body["schema"]) == 14

        # Every schema entry has the 7 documented fields.
        for key, entry in body["schema"].items():
            assert set(entry.keys()) == {
                "default", "min", "max", "step",
                "value_type", "description", "code_location",
            }, f"unexpected schema fields for {key}: {entry.keys()}"

        # Values reflect each entry's default at startup.
        for key, entry in body["schema"].items():
            assert body["values"][key] == entry["default"], key
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_patch_valid_emits_events_and_updates_store(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/patch",
                json={"key": "policy.backchannel_threshold", "value": 0.5},
            )
            assert resp.status == 200, await resp.text()
            body = await resp.json()

        assert body["key"] == "policy.backchannel_threshold"
        assert body["previous_value"] == 0.7
        assert body["new_value"] == 0.5
        op_id = body["operator_action_event_id"]
        cc_id = body["config_change_event_id"]
        assert op_id and cc_id

        # ConfigStore actually updated.
        config_store = app[KEY_CONFIG_STORE]
        assert config_store.get("policy.backchannel_threshold") == 0.5

        # Flush logger drain so the subscriber sees the events.
        await _flush(app[KEY_LOGGER])

        # Exactly one operator_action + one config_change should have fired.
        op_events = [e for e in events if e.event_type == "operator_action"]
        cc_events = [e for e in events if e.event_type == "config_change"]
        assert len(op_events) == 1
        assert len(cc_events) == 1
        assert op_events[0].event_id == op_id
        assert cc_events[0].event_id == cc_id

        # operator_action is a DAG root.
        assert op_events[0].caused_by == []
        # config_change closes the DAG via the operator_action event id.
        assert cc_events[0].caused_by == [op_id]

        # Payload contents.
        op_payload = op_events[0].payload_inline
        assert op_payload is not None
        assert op_payload["endpoint"] == "/config/patch"
        assert "client_ip" in op_payload
        assert "request_id" in op_payload

        cc_payload = cc_events[0].payload_inline
        assert cc_payload is not None
        assert cc_payload["key"] == "policy.backchannel_threshold"
        assert cc_payload["previous_value"] == 0.7
        assert cc_payload["new_value"] == 0.5
        assert cc_payload["operator_action_event_id"] == op_id
        assert isinstance(cc_payload["applied_at_ms"], int)
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_patch_tier_a_key_returns_403(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/patch",
                json={"key": "POLICY_VERSION", "value": 1},
            )
            assert resp.status == 403
            body = await resp.json()
        assert body["tier"] == "A"
        assert body["key"] == "POLICY_VERSION"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_patch_unknown_key_returns_403(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/patch",
                json={"key": "nonsense.not.a.thing", "value": 0.42},
            )
            assert resp.status == 403
            body = await resp.json()
        assert body["tier"] == "unknown"
        assert body["key"] == "nonsense.not.a.thing"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_patch_out_of_range_returns_400(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            # policy.backchannel_threshold range is [0.40, 0.95]; 1.5 is above.
            resp = await session.post(
                f"{base}/config/patch",
                json={"key": "policy.backchannel_threshold", "value": 1.5},
            )
            assert resp.status == 400
            body = await resp.json()
        assert body["tier"] == "B"
        assert body["key"] == "policy.backchannel_threshold"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_patch_bool_value_for_int_key_returns_400(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            # detectors.vad.silence_onset_ms wants int; bool must be rejected
            # (validate_patch's explicit bool gate from PR #146).
            resp = await session.post(
                f"{base}/config/patch",
                json={"key": "detectors.vad.silence_onset_ms", "value": True},
            )
            assert resp.status == 400
            body = await resp.json()
        assert body["tier"] == "B"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_reset_single_key_resets_to_default(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            # First patch so the value is non-default.
            await session.post(
                f"{base}/config/patch",
                json={"key": "policy.backchannel_threshold", "value": 0.55},
            )
            # Now reset.
            resp = await session.post(
                f"{base}/config/reset",
                json={"key": "policy.backchannel_threshold"},
            )
            assert resp.status == 200
            body = await resp.json()

        await _flush(app[KEY_LOGGER])

        assert app[KEY_CONFIG_STORE].get("policy.backchannel_threshold") == 0.7
        assert len(body["changes"]) == 1
        assert body["changes"][0]["new_value"] == 0.7

        # Should have: patch's (op + cc), reset's (op + cc) = 4 events.
        op_events = [e for e in events if e.event_type == "operator_action"]
        cc_events = [e for e in events if e.event_type == "config_change"]
        assert len(op_events) == 2
        assert len(cc_events) == 2

        # The reset's config_change closes the DAG against its operator_action.
        reset_cc_ids = body["config_change_event_ids"]
        assert len(reset_cc_ids) == 1
        reset_cc = next(e for e in cc_events if e.event_id == reset_cc_ids[0])
        assert reset_cc.caused_by == [body["operator_action_event_id"]]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_reset_single_key_already_default_emits_only_operator_action(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            # Reset a key that's already at default.
            resp = await session.post(
                f"{base}/config/reset",
                json={"key": "policy.backchannel_threshold"},
            )
            assert resp.status == 200
            body = await resp.json()

        await _flush(app[KEY_LOGGER])

        # No actual change → 1 operator_action, 0 config_change.
        assert body["changes"] == []
        assert body["config_change_event_ids"] == []

        op_events = [e for e in events if e.event_type == "operator_action"]
        cc_events = [e for e in events if e.event_type == "config_change"]
        assert len(op_events) == 1
        assert len(cc_events) == 0
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_reset_section_resets_all_keys_in_prefix(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            # Patch all three policy keys away from default.
            policy_keys = [k for k in ALLOWLIST.keys() if k.startswith("policy.")]
            assert len(policy_keys) == 3
            for k in policy_keys:
                # Bump each by a small in-range delta.
                resp = await session.post(
                    f"{base}/config/patch",
                    json={"key": k, "value": ALLOWLIST[k].default - 0.1},
                )
                assert resp.status == 200

            # Reset the whole policy.* section.
            resp = await session.post(
                f"{base}/config/reset",
                json={"section": "policy"},
            )
            assert resp.status == 200
            body = await resp.json()

        await _flush(app[KEY_LOGGER])

        # All 3 policy keys back to default.
        for k in policy_keys:
            assert app[KEY_CONFIG_STORE].get(k) == ALLOWLIST[k].default

        assert len(body["changes"]) == 3
        assert len(body["config_change_event_ids"]) == 3

        # Reset emitted exactly 1 operator_action + 3 config_change.
        reset_op_id = body["operator_action_event_id"]
        cc_events = [e for e in events if e.event_type == "config_change"]
        reset_cc_events = [
            e for e in cc_events
            if e.event_id in body["config_change_event_ids"]
        ]
        assert len(reset_cc_events) == 3
        for e in reset_cc_events:
            assert e.caused_by == [reset_op_id]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_post_config_reset_global_resets_all_changed_keys(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            # Patch two keys away from default.
            await session.post(
                f"{base}/config/patch",
                json={"key": "policy.backchannel_threshold", "value": 0.6},
            )
            await session.post(
                f"{base}/config/patch",
                json={"key": "detectors.vad.silence_onset_ms", "value": 400},
            )

            # Empty-body global reset.
            resp = await session.post(f"{base}/config/reset", json={})
            assert resp.status == 200
            body = await resp.json()

        await _flush(app[KEY_LOGGER])

        # All 12 keys back to default.
        for k, entry in ALLOWLIST.items():
            assert app[KEY_CONFIG_STORE].get(k) == entry.default

        # Only the 2 changed keys produce config_change events on this reset.
        assert len(body["changes"]) == 2
        assert len(body["config_change_event_ids"]) == 2
        reset_op_id = body["operator_action_event_id"]

        cc_events = [e for e in events if e.event_type == "config_change"]
        reset_ccs = [
            e for e in cc_events
            if e.event_id in body["config_change_event_ids"]
        ]
        assert len(reset_ccs) == 2
        for e in reset_ccs:
            assert e.caused_by == [reset_op_id]
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_operator_action_caused_by_is_empty(tmp_path: Path) -> None:
    """operator_action is the root cause for an operator-initiated chain."""
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/patch",
                json={"key": "policy.backchannel_threshold", "value": 0.5},
            )
            assert resp.status == 200

        await _flush(app[KEY_LOGGER])

        op_events = [e for e in events if e.event_type == "operator_action"]
        assert len(op_events) == 1
        assert op_events[0].caused_by == []
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_config_change_caused_by_matches_operator_action(tmp_path: Path) -> None:
    """config_change.caused_by must list the upstream operator_action's id."""
    app = _make_app(tmp_path)
    events = _subscribe_event_capture(app)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            resp = await session.post(
                f"{base}/config/patch",
                json={"key": "detectors.vad.silence_onset_ms", "value": 250},
            )
            assert resp.status == 200
            body = await resp.json()

        await _flush(app[KEY_LOGGER])

        cc_events = [e for e in events if e.event_type == "config_change"]
        assert len(cc_events) == 1
        assert cc_events[0].caused_by == [body["operator_action_event_id"]]
    finally:
        await server.close()
