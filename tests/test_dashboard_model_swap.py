"""Contract tests — per-seam HTTP round-trip and disabled=None invariant (Dashboard P1 D5).

Per plan §D5 implementation sketch:
  1. Parametrized test, one case per seam in HOT_SEAMS.
  2. Disabled-seam factory returns None (F2): ConfigStore.get_seam(seam) returns False
     after set_seam(seam, False).
  3. Unknown-seam POST returns 403 (F6: cold seams rejected as unknown_seam).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer

from manual_test_console.config_schema import HOT_SEAMS
from manual_test_console.config_store import ConfigStore
from manual_test_console.server import KEY_CONFIG_STORE, build_app


def _make_app(tmp_path: Path):
    return build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)


async def _flush(logger, *, rounds: int = 10) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


@pytest.mark.parametrize("seam", HOT_SEAMS)
@pytest.mark.asyncio
async def test_seam_round_trip(seam: str, tmp_path: Path) -> None:
    """POST disable → GET seams shows disabled; POST enable → GET shows enabled."""
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            # Disable the seam.
            resp = await session.post(
                f"{base}/config/model-swap",
                json={"seam": seam, "enabled": False},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["accepted"] is True

            # Confirm via GET /config/seams.
            resp2 = await session.get(f"{base}/config/seams")
            seam_map = {
                entry["seam"]: entry["enabled"]
                for entry in (await resp2.json())["seams"]
            }
            assert seam_map[seam] is False, f"{seam} should be disabled"
            # All other seams remain enabled.
            for other_seam in HOT_SEAMS:
                if other_seam != seam:
                    assert seam_map[other_seam] is True, f"{other_seam} should still be enabled"

            # Re-enable.
            resp3 = await session.post(
                f"{base}/config/model-swap",
                json={"seam": seam, "enabled": True},
            )
            assert resp3.status == 200
            assert (await resp3.json())["accepted"] is True

            resp4 = await session.get(f"{base}/config/seams")
            seam_map2 = {
                entry["seam"]: entry["enabled"]
                for entry in (await resp4.json())["seams"]
            }
            assert seam_map2[seam] is True, f"{seam} should be re-enabled"
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_disabled_seam_factory_returns_false_in_store(tmp_path: Path) -> None:
    """F2: disabled seam → ConfigStore.get_seam returns False (factory produces None path).

    The live-pipeline factory reads get_seam() to decide whether to pass None
    for a factory arg. This test asserts the ConfigStore contract: set_seam(seam,
    False) → get_seam(seam) returns False. That is the signal the factory uses to
    return None per F2.
    """
    from manual_test_console.config_schema import ALLOWLIST

    store = ConfigStore(ALLOWLIST)
    # All seams default to True (enabled).
    for seam in HOT_SEAMS:
        assert store.get_seam(seam) is True

    # set_seam to False → get_seam returns False.
    change = store.set_seam("vad", False)
    assert change.previous_enabled is True
    assert change.new_enabled is False
    assert store.get_seam("vad") is False

    # Other seams unaffected.
    for seam in HOT_SEAMS:
        if seam != "vad":
            assert store.get_seam(seam) is True


@pytest.mark.asyncio
async def test_cold_seam_rejected_as_unknown(tmp_path: Path) -> None:
    """F6: non-hot-seam POST returns 403 unknown_seam."""
    app = _make_app(tmp_path)
    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            for cold_seam in ("foreground", "tts_native", "cold_seam_bogus"):
                resp = await session.post(
                    f"{base}/config/model-swap",
                    json={"seam": cold_seam, "enabled": False},
                )
                assert resp.status == 403, f"{cold_seam} should be 403"
                body = await resp.json()
                assert "model_swap_event_id" in body
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_two_state_seam_invariant_via_config_store(tmp_path: Path) -> None:
    """seam_state ∈ {True, False} only — ConfigStore level.

    Asserts that every seam in current_seam_state() is a strict bool,
    not an int or other truthy value.
    """
    from manual_test_console.config_schema import ALLOWLIST

    store = ConfigStore(ALLOWLIST)
    state = store.current_seam_state()
    assert set(state.keys()) == set(HOT_SEAMS)
    for seam_name, val in state.items():
        assert type(val) is bool, f"{seam_name}: expected bool, got {type(val).__name__}"
        assert val in (True, False)

    # After toggling, still bool.
    store.set_seam("asr", False)
    state2 = store.current_seam_state()
    assert type(state2["asr"]) is bool
    assert state2["asr"] is False


@pytest.mark.asyncio
async def test_model_swap_audit_completeness(tmp_path: Path) -> None:
    """model_swap_audit_completeness == 1.0: 24 swaps → 24 model_swap_completed events, 0 rejected.

    Per plan §D5 test plan: drive 24 swaps (12 seams × {True, False}).
    """
    from companion_harness.schemas import Event
    from manual_test_console.server import KEY_LOGGER

    app = _make_app(tmp_path)
    events: list[Event] = []

    async def _capture(evt: Event) -> None:
        events.append(evt)

    app[KEY_LOGGER].subscribe(_capture)

    server = TestServer(app)
    await server.start_server()
    try:
        base = f"http://{server.host}:{server.port}"
        async with ClientSession() as session:
            for seam in HOT_SEAMS:
                for enabled in (False, True):
                    resp = await session.post(
                        f"{base}/config/model-swap",
                        json={"seam": seam, "enabled": enabled},
                    )
                    assert resp.status == 200

        await _flush(app[KEY_LOGGER], rounds=20)

        completed = [e for e in events if e.event_type == "model_swap_completed"]
        rejected = [e for e in events if e.event_type == "model_swap_rejected"]
        assert len(completed) == 24, f"expected 24 completed, got {len(completed)}"
        assert len(rejected) == 0, f"expected 0 rejected, got {len(rejected)}"
    finally:
        await server.close()
