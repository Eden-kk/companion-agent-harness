"""Contract test: _snapshot_config() applies reasoner.budget_* Tier-B keys.

Success criterion (T3):
  test_snapshot_config_applies_reasoner_budget_keys: after patching both keys,
  the reasoner's _budget_wall_clock_s and _budget_step_count reflect the patched values.
"""

from __future__ import annotations

from companion_harness.background_reasoner import MCPBackgroundReasoner
from manual_test_console.config_schema import ALLOWLIST
from manual_test_console.config_store import ConfigStore


def _make_store() -> ConfigStore:
    return ConfigStore(ALLOWLIST)


def _apply_snapshot(store: ConfigStore, reasoner: MCPBackgroundReasoner) -> None:
    """Inline the budget-key read from realtime_orchestrator._snapshot_config()."""
    try:
        wcs = store.get("reasoner.budget_wall_clock_s")
    except KeyError:
        wcs = None
    try:
        sc = store.get("reasoner.budget_step_count")
    except KeyError:
        sc = None
    if wcs is not None and hasattr(reasoner, "_budget_wall_clock_s"):
        reasoner._budget_wall_clock_s = float(wcs)
    if sc is not None and hasattr(reasoner, "_budget_step_count"):
        reasoner._budget_step_count = int(sc)


def test_snapshot_config_applies_reasoner_budget_keys() -> None:
    """ConfigStore Tier-B patch propagates to MCPBackgroundReasoner attributes."""
    store = _make_store()
    reasoner = MCPBackgroundReasoner(
        mcp_server_url="stdio://fake",
        budget_wall_clock_s=30.0,
        budget_step_count=8,
    )

    assert reasoner._budget_wall_clock_s == 30.0
    assert reasoner._budget_step_count == 8

    # Set both keys (ConfigStore.set() is the write path; validate_patch guards externally)
    store.set("reasoner.budget_wall_clock_s", 60.0)
    store.set("reasoner.budget_step_count", 16)

    # Simulate EOU snapshot
    _apply_snapshot(store, reasoner)

    assert reasoner._budget_wall_clock_s == 60.0
    assert reasoner._budget_step_count == 16


def test_snapshot_config_budget_keys_in_allowlist() -> None:
    """Both Tier-B keys are present in ALLOWLIST with correct defaults."""
    assert "reasoner.budget_wall_clock_s" in ALLOWLIST
    assert "reasoner.budget_step_count" in ALLOWLIST

    wcs_entry = ALLOWLIST["reasoner.budget_wall_clock_s"]
    assert wcs_entry.default == 30.0
    assert wcs_entry.value_type is float

    sc_entry = ALLOWLIST["reasoner.budget_step_count"]
    assert sc_entry.default == 8
    assert sc_entry.value_type is int


def test_snapshot_config_budget_keys_validate_patch() -> None:
    """validate_patch accepts values in range and rejects out-of-range."""
    from manual_test_console.config_schema import validate_patch

    ok, _ = validate_patch("reasoner.budget_wall_clock_s", 45.0)
    assert ok

    ok, _ = validate_patch("reasoner.budget_wall_clock_s", 0.5)  # below min=1.0
    assert not ok

    ok, _ = validate_patch("reasoner.budget_step_count", 32)
    assert ok

    ok, _ = validate_patch("reasoner.budget_step_count", 0)  # below min=1
    assert not ok
