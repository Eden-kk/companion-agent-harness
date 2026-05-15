"""Tests for ``manual_test_console.config_store``.

The store is decoupled from the concrete allowlist (Task B); these tests use
a minimal in-memory stub allowlist entry that mirrors the
``TierBSchemaEntry``-style structural shape (``.default``, ``.min``, ``.max``,
``.value_type``) the production allowlist will provide.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import pytest

from manual_test_console.config_store import ConfigChange, ConfigStore


@dataclass(frozen=True)
class _MockEntry:
    default: float | int
    min: float | int = 0
    max: float | int = 1
    value_type: type = float


def _make_allowlist() -> dict[str, _MockEntry]:
    return {
        "policy.backchannel_threshold": _MockEntry(default=0.7, min=0.4, max=0.95),
        "detectors.vad.silence_onset_ms": _MockEntry(
            default=300, min=150, max=800, value_type=int
        ),
    }


def test_init_populates_state_from_defaults():
    store = ConfigStore(_make_allowlist())

    assert store.get("policy.backchannel_threshold") == 0.7
    assert store.get("detectors.vad.silence_onset_ms") == 300


def test_get_unknown_key_raises_keyerror():
    store = ConfigStore(_make_allowlist())

    with pytest.raises(KeyError):
        store.get("not.a.real.key")


def test_current_state_returns_snapshot_copy():
    store = ConfigStore(_make_allowlist())

    snapshot = store.current_state()
    assert snapshot == {
        "policy.backchannel_threshold": 0.7,
        "detectors.vad.silence_onset_ms": 300,
    }

    # Mutating the snapshot must not affect the store.
    snapshot["policy.backchannel_threshold"] = 99.9
    assert store.get("policy.backchannel_threshold") == 0.7


def test_set_updates_state_and_returns_change():
    store = ConfigStore(_make_allowlist())

    change = store.set("policy.backchannel_threshold", 0.55)

    assert change == ConfigChange(
        key="policy.backchannel_threshold",
        previous_value=0.7,
        new_value=0.55,
    )
    assert store.get("policy.backchannel_threshold") == 0.55


def test_set_unknown_key_raises_keyerror():
    store = ConfigStore(_make_allowlist())

    with pytest.raises(KeyError):
        store.set("not.a.real.key", 1.0)


def test_reset_returns_change_when_value_differs():
    store = ConfigStore(_make_allowlist())
    store.set("policy.backchannel_threshold", 0.5)

    change = store.reset("policy.backchannel_threshold")

    assert change == ConfigChange(
        key="policy.backchannel_threshold",
        previous_value=0.5,
        new_value=0.7,
    )
    assert store.get("policy.backchannel_threshold") == 0.7


def test_reset_returns_none_when_already_at_default():
    store = ConfigStore(_make_allowlist())

    assert store.reset("policy.backchannel_threshold") is None


def test_reset_unknown_key_raises_keyerror():
    store = ConfigStore(_make_allowlist())

    with pytest.raises(KeyError):
        store.reset("not.a.real.key")


def test_reset_all_returns_changes_for_only_modified_keys():
    store = ConfigStore(_make_allowlist())
    store.set("policy.backchannel_threshold", 0.5)
    # detectors.vad.silence_onset_ms is left at default

    changes = store.reset_all()

    assert changes == [
        ConfigChange(
            key="policy.backchannel_threshold",
            previous_value=0.5,
            new_value=0.7,
        ),
    ]
    assert store.current_state() == {
        "policy.backchannel_threshold": 0.7,
        "detectors.vad.silence_onset_ms": 300,
    }


def test_reset_all_resets_every_modified_key():
    store = ConfigStore(_make_allowlist())
    store.set("policy.backchannel_threshold", 0.5)
    store.set("detectors.vad.silence_onset_ms", 500)

    changes = store.reset_all()

    assert len(changes) == 2
    assert {c.key for c in changes} == {
        "policy.backchannel_threshold",
        "detectors.vad.silence_onset_ms",
    }
    assert store.current_state() == {
        "policy.backchannel_threshold": 0.7,
        "detectors.vad.silence_onset_ms": 300,
    }


def test_load_from_yaml_applies_leaf_values(tmp_path: Path):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "policy:\n"
        "  backchannel_threshold: 0.55\n"
        "detectors:\n"
        "  vad:\n"
        "    silence_onset_ms: 250\n",
    )
    store = ConfigStore(_make_allowlist())

    changes = store.load_from_yaml(yaml_path)

    assert len(changes) == 2
    by_key = {c.key: c for c in changes}
    assert by_key["policy.backchannel_threshold"].new_value == 0.55
    assert by_key["policy.backchannel_threshold"].previous_value == 0.7
    assert by_key["detectors.vad.silence_onset_ms"].new_value == 250
    assert by_key["detectors.vad.silence_onset_ms"].previous_value == 300
    assert store.get("policy.backchannel_threshold") == 0.55
    assert store.get("detectors.vad.silence_onset_ms") == 250


def test_load_from_yaml_missing_file_is_noop(tmp_path: Path):
    store = ConfigStore(_make_allowlist())
    missing = tmp_path / "does-not-exist.yaml"

    changes = store.load_from_yaml(missing)

    assert changes == []
    # Defaults are untouched.
    assert store.get("policy.backchannel_threshold") == 0.7
    assert store.get("detectors.vad.silence_onset_ms") == 300


def test_load_from_yaml_unknown_key_logs_warning_and_skips(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "policy:\n"
        "  backchannel_threshold: 0.55\n"
        "mystery:\n"
        "  unknown_knob: 42\n",
    )
    store = ConfigStore(_make_allowlist())

    with caplog.at_level(logging.WARNING, logger="manual_test_console.config_store"):
        changes = store.load_from_yaml(yaml_path)

    # Only the known key was applied.
    assert len(changes) == 1
    assert changes[0].key == "policy.backchannel_threshold"
    # The unknown key was logged.
    assert any(
        "mystery.unknown_knob" in record.getMessage()
        and record.levelno == logging.WARNING
        for record in caplog.records
    )
    # Defaults preserved for unknown key path; known key applied.
    assert store.get("policy.backchannel_threshold") == 0.55


def test_load_from_yaml_non_dict_root_logs_and_returns_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text("- just\n- a\n- list\n")
    store = ConfigStore(_make_allowlist())

    with caplog.at_level(logging.WARNING, logger="manual_test_console.config_store"):
        changes = store.load_from_yaml(yaml_path)

    assert changes == []
    assert store.get("policy.backchannel_threshold") == 0.7
    assert any(
        "does not parse to dict" in record.getMessage()
        for record in caplog.records
    )
