"""Tests for CoreUserProfileStore (v0.1e Task 8).

Success criterion: core profile facts survive process restart.
"""

import pathlib

import pytest

from companion_harness.core_user_profile_store import CoreUserProfileStore
from companion_harness.memory_manager import MemoryManager
from companion_harness.schemas import MemoryItem, SensitiveField


def _make_item(item_id: str = "item-001", **overrides) -> MemoryItem:
    defaults = dict(
        item_id=item_id,
        store="core_profile",
        content={"fact": "user name is Alice"},
        source_event_id="evt-001",
        created_at="2026-05-15T00:00:00Z",
        last_confirmed_at="2026-05-15T00:00:00Z",
        confidence=0.99,
        salience=0.8,
        privacy_level="safe",
        mutability="user_only",
        valid_from="2026-05-15T00:00:00Z",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="default", value="user's name"
        ),
    )
    defaults.update(overrides)
    return MemoryItem(**defaults)


def test_commit_then_retrieve_same_process(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    item = _make_item()
    store.commit(item)
    results = store.retrieve("Alice")
    assert len(results) == 1
    assert results[0].item_id == "item-001"


def test_satisfies_memory_manager_protocol(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    assert isinstance(store, MemoryManager)


def test_persists_across_instances(tmp_path: pathlib.Path) -> None:
    profile_path = tmp_path / "profile.json"
    store_a = CoreUserProfileStore(profile_path)
    store_a.commit(_make_item())

    store_b = CoreUserProfileStore(profile_path)
    results = store_b.retrieve("Alice")
    assert len(results) == 1
    assert results[0].item_id == "item-001"


def test_atomic_write_no_partial_state(tmp_path: pathlib.Path) -> None:
    profile_path = tmp_path / "profile.json"
    store = CoreUserProfileStore(profile_path)
    store.commit(_make_item())
    tmp_file = profile_path.with_suffix(profile_path.suffix + ".tmp")
    assert not tmp_file.exists()


def test_provenance_round_trip(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    item = _make_item(
        item_id="item-prov",
        content={"fact": "pronouns are they/them", "role": "researcher"},
        source_event_id="evt-prov",
        created_at="2026-01-01T12:00:00Z",
        last_confirmed_at="2026-03-01T08:00:00Z",
        confidence=0.95,
        salience=0.7,
        valid_from="2026-01-01T12:00:00Z",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="pii-30d",
            value="user's pronouns",
            sensitivity="sensitive",
        ),
    )
    store.commit(item)
    results = store.retrieve("pronouns")
    assert len(results) == 1
    r = results[0]
    assert r.item_id == "item-prov"
    assert r.content == {"fact": "pronouns are they/them", "role": "researcher"}
    assert r.created_at == "2026-01-01T12:00:00Z"
    assert r.confidence == pytest.approx(0.95)
    assert r.valid_to is None
    assert r.superseded_by is None
    assert r.user_visible_summary.retention_policy_id == "pii-30d"
    assert r.user_visible_summary.value == "user's pronouns"
    assert r.user_visible_summary.sensitivity == "sensitive"
