"""Contract tests for ConfigStore seam-state namespace (D1)."""

from __future__ import annotations

import pytest

from manual_test_console.config_schema import ALLOWLIST, HOT_SEAMS
from manual_test_console.config_store import ConfigStore, SeamStateChange


def _make_store(**seam_defaults: bool) -> ConfigStore:
    return ConfigStore(ALLOWLIST, seam_defaults=seam_defaults or None)


def test_default_state_all_seams_enabled():
    store = _make_store()
    state = store.current_seam_state()
    assert set(state.keys()) == set(HOT_SEAMS)
    assert all(state[s] for s in HOT_SEAMS)


def test_get_seam_returns_true_by_default():
    store = _make_store()
    assert store.get_seam("vad") is True
    assert store.get_seam("tts") is True


def test_set_seam_flips_state_and_returns_change():
    store = _make_store()
    change = store.set_seam("vad", False)
    assert change == SeamStateChange(seam="vad", previous_enabled=True, new_enabled=False)
    assert store.get_seam("vad") is False


def test_set_seam_unknown_raises_keyerror():
    store = _make_store()
    with pytest.raises(KeyError):
        store.set_seam("nonsense", True)


def test_get_seam_unknown_raises_keyerror():
    store = _make_store()
    with pytest.raises(KeyError):
        store.get_seam("nonsense")


def test_seam_defaults_constructor_initializes_disabled():
    store = ConfigStore(ALLOWLIST, seam_defaults={"vad": False})
    assert store.get_seam("vad") is False
    assert store.get_seam("tts") is True


def test_seam_defaults_unknown_key_ignored():
    # Unknown keys in seam_defaults are silently ignored (not in HOT_SEAMS).
    store = ConfigStore(ALLOWLIST, seam_defaults={"nonexistent": False})
    state = store.current_seam_state()
    assert all(state[s] for s in HOT_SEAMS)


def test_current_seam_state_is_snapshot():
    store = _make_store()
    snapshot = store.current_seam_state()
    snapshot["vad"] = False
    assert store.get_seam("vad") is True


def test_hot_seams_has_twelve_entries():
    assert len(HOT_SEAMS) == 12


def test_tier_b_state_unaffected_by_seam_operations():
    store = _make_store()
    store.set_seam("vad", False)
    # Tier-B state must remain intact.
    assert store.get("policy.backchannel_threshold") == 0.7
