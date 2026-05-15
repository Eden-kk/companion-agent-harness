"""Contract tests for manual_test_console.config_schema.

Verifies the Tier-B allowlist has exactly 12 entries with required metadata
and that validate_patch enforces:
  - key membership in the allowlist
  - value-type match (rejects ints when float expected — and vice versa —
    plus rejects bool entirely since it would slip past int type checks)
  - [min, max] range check

Also verifies the Tier-A frozenset helper.

See docs/design-config-and-dashboard.md §1 (Tier B audit) and §5
(Server API → TierBSchemaEntry).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from manual_test_console.config_schema import (
    ALLOWLIST,
    TierBSchemaEntry,
    tier_a_keys,
    validate_patch,
)


# --- ALLOWLIST shape ---------------------------------------------------------


def test_allowlist_has_exactly_twelve_entries():
    """Design doc §1 Tier B audit table = 12 keys."""
    assert len(ALLOWLIST) == 12, (
        f"expected 12 Tier-B entries, got {len(ALLOWLIST)}: "
        f"{sorted(ALLOWLIST.keys())}"
    )


def test_allowlist_keys_are_canonical():
    """Each entry's `key` attribute matches its dict key."""
    for dict_key, entry in ALLOWLIST.items():
        assert entry.key == dict_key, (
            f"dict key {dict_key!r} != entry.key {entry.key!r}"
        )


def test_each_entry_has_required_metadata():
    """Each TierBSchemaEntry has every documented field populated."""
    for key, entry in ALLOWLIST.items():
        assert isinstance(entry, TierBSchemaEntry), key
        assert entry.code_location, f"{key} missing code_location"
        assert ":" in entry.code_location, (
            f"{key} code_location must be 'file.py:lineno', got "
            f"{entry.code_location!r}"
        )
        assert entry.default is not None, f"{key} missing default"
        assert entry.min is not None, f"{key} missing min"
        assert entry.max is not None, f"{key} missing max"
        assert entry.step is not None, f"{key} missing step"
        assert entry.value_type in (float, int), (
            f"{key} value_type must be float or int, got {entry.value_type}"
        )
        assert entry.description, f"{key} missing description"
        # Default must be inside [min, max] — a default outside its slider
        # range is a definite spec authoring bug.
        assert entry.min <= entry.default <= entry.max, (
            f"{key}: default {entry.default} outside [{entry.min}, {entry.max}]"
        )


def test_value_type_matches_default_type():
    """default literal type matches the declared value_type."""
    for key, entry in ALLOWLIST.items():
        if entry.value_type is float:
            assert isinstance(entry.default, float), (
                f"{key} value_type=float but default {entry.default!r} is not float"
            )
        elif entry.value_type is int:
            assert isinstance(entry.default, int) and not isinstance(
                entry.default, bool
            ), f"{key} value_type=int but default {entry.default!r} is not int"


def test_dataclass_is_frozen():
    """TierBSchemaEntry must be frozen (immutable schema)."""
    entry = next(iter(ALLOWLIST.values()))
    with pytest.raises(Exception):
        entry.default = 0.0  # type: ignore[misc]


# --- validate_patch: accept paths -------------------------------------------


def test_validate_patch_accepts_default_for_every_key():
    """Every key accepts its own default as a valid patch."""
    for key, entry in ALLOWLIST.items():
        ok, err = validate_patch(key, entry.default)
        assert ok, f"{key}: default {entry.default} rejected with {err!r}"
        assert err == ""


def test_validate_patch_accepts_min_and_max_for_every_key():
    """Boundary values (min, max) are inclusive."""
    for key, entry in ALLOWLIST.items():
        ok_min, _ = validate_patch(key, entry.min)
        assert ok_min, f"{key}: min={entry.min} should be accepted (inclusive)"
        ok_max, _ = validate_patch(key, entry.max)
        assert ok_max, f"{key}: max={entry.max} should be accepted (inclusive)"


# --- validate_patch: reject paths -------------------------------------------


def test_validate_patch_rejects_unknown_key():
    """Keys not in ALLOWLIST are rejected with an explanatory message."""
    ok, err = validate_patch("policy.does_not_exist", 0.5)
    assert not ok
    assert "allowlist" in err.lower()


def test_validate_patch_rejects_tier_a_keys_as_unknown():
    """Tier-A names aren't in the Tier-B allowlist either."""
    ok, err = validate_patch("POLICY_VERSION", "v0.1d")
    assert not ok
    assert "allowlist" in err.lower()


def test_validate_patch_rejects_out_of_range_below_min():
    """Backchannel threshold (min 0.40): 0.39 rejected."""
    ok, err = validate_patch("policy.backchannel_threshold", 0.39)
    assert not ok
    assert "range" in err.lower()


def test_validate_patch_rejects_out_of_range_above_max():
    """Backchannel threshold (max 0.95): 0.96 rejected."""
    ok, err = validate_patch("policy.backchannel_threshold", 0.96)
    assert not ok
    assert "range" in err.lower()


def test_validate_patch_rejects_silence_onset_below_min():
    """Second sample key: VAD silence_onset_ms (min 150): 149 rejected."""
    ok, err = validate_patch("detectors.vad.silence_onset_ms", 149)
    assert not ok
    assert "range" in err.lower()


def test_validate_patch_rejects_silence_onset_above_max():
    """VAD silence_onset_ms (max 800): 801 rejected."""
    ok, err = validate_patch("detectors.vad.silence_onset_ms", 801)
    assert not ok
    assert "range" in err.lower()


def test_validate_patch_rejects_hard_cancel_above_cap():
    """orchestrator.hard_cancel_after_ms is capped at 180 (design doc §11 OQ-1)."""
    ok, err = validate_patch("orchestrator.hard_cancel_after_ms", 181)
    assert not ok
    assert "range" in err.lower()


def test_validate_patch_rejects_float_when_int_expected():
    """Type mismatch: VAD silence_onset_ms is int; 300.0 rejected."""
    ok, err = validate_patch("detectors.vad.silence_onset_ms", 300.0)
    assert not ok
    assert "type mismatch" in err.lower()


def test_validate_patch_rejects_string_value():
    """Type mismatch: floats can't be strings."""
    ok, err = validate_patch("policy.backchannel_threshold", "0.7")
    assert not ok
    assert "type mismatch" in err.lower()


def test_validate_patch_rejects_bool_value():
    """bool is a subclass of int in Python; reject it explicitly so that a
    slider value can never be True/False."""
    ok, err = validate_patch("detectors.vad.silence_onset_ms", True)
    assert not ok
    assert "type mismatch" in err.lower()


# --- tier_a_keys -------------------------------------------------------------


def test_tier_a_keys_returns_frozenset():
    keys = tier_a_keys()
    assert isinstance(keys, frozenset)


def test_tier_a_keys_includes_policy_version_when_nonempty():
    """Design doc §1 Tier A: POLICY_VERSION is the canonical example."""
    keys = tier_a_keys()
    if keys:
        assert "POLICY_VERSION" in keys


def test_tier_a_and_tier_b_keys_are_disjoint():
    """A key can be exactly one of Tier A / Tier B (design doc §1)."""
    overlap = tier_a_keys() & ALLOWLIST.keys()
    assert not overlap, f"keys appear in both tiers: {overlap}"


# --- Smoke: config.yaml file is shippable -----------------------------------


def test_config_yaml_file_exists_and_is_valid_yaml():
    """The companion config.yaml ships with the schema module and is parseable."""
    yaml = pytest.importorskip("yaml")
    config_path = (
        Path(__file__).resolve().parents[1]
        / "manual_test_console"
        / "config.yaml"
    )
    assert config_path.exists(), f"missing {config_path}"
    data = yaml.safe_load(config_path.read_text())
    assert isinstance(data, dict)
    # Top-level sections per design doc §2 / task spec.
    for section in ("policy", "detectors", "orchestrator"):
        assert section in data, f"config.yaml missing top-level section {section!r}"
