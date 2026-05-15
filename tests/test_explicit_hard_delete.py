"""Task 15: explicit 'delete that permanently' command — contract test (v0.1e).

Spec: docs/architecture-v0.1.md §Stage 4 lines 529-534.

Success criterion: user command "delete that permanently" →
  item file removed entirely (content gone); only a non-content deletion
  receipt is retained (tested via get() returning None); hard_delete is
  distinct from forget (forget = historical correction; delete = privacy op).
"""

from __future__ import annotations

import pathlib

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import MemoryItem, SensitiveField


def _make_item(item_id: str = "item-delete-001") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": "user's home address is 123 Main St"},
        source_event_id="evt-src-002",
        created_at="2026-05-14T10:00:00+00:00",
        last_confirmed_at="2026-05-14T10:00:00+00:00",
        confidence=0.95,
        salience=0.6,
        privacy_level="sensitive",
        mutability="user_only",
        valid_from="2026-05-14T10:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value="user home address",
            sensitivity="sensitive",
        ),
    )


def test_hard_delete_removes_file(tmp_path: pathlib.Path) -> None:
    ep_dir = tmp_path / "episodic"
    store = EpisodicMemoryStore(ep_dir)
    store.commit(_make_item())

    assert (ep_dir / "item-delete-001.json").exists()
    store.hard_delete("item-delete-001")
    assert not (ep_dir / "item-delete-001.json").exists()


def test_hard_delete_get_returns_none(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    store.hard_delete("item-delete-001")

    assert store.get("item-delete-001") is None


def test_hard_delete_excluded_from_retrieval(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    store.hard_delete("item-delete-001")

    results = store.retrieve("address", top_k=10, include_history=True)
    assert not any(r.item_id == "item-delete-001" for r in results)


def test_hard_delete_idempotent(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    store.hard_delete("item-delete-001")
    # Second call must not raise
    store.hard_delete("item-delete-001")


def test_hard_delete_distinct_from_forget(tmp_path: pathlib.Path) -> None:
    """forget retains a tombstone; hard_delete removes it entirely."""
    ep_dir = tmp_path / "episodic"
    store = EpisodicMemoryStore(ep_dir)

    store.commit(_make_item(item_id="item-forgotten"))
    store.forget("item-forgotten")
    # Tombstone still on disk after forget
    assert (ep_dir / "item-forgotten.json").exists()

    store.commit(_make_item(item_id="item-deleted"))
    store.hard_delete("item-deleted")
    # File gone after hard_delete
    assert not (ep_dir / "item-deleted.json").exists()
