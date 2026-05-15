"""Task 14: explicit 'forget that' command — contract test (v0.1e).

Spec: docs/architecture-v0.1.md §Stage 4 lines 523-527.

Success criterion: user command "forget that" →
  target item's valid_to is set; superseded_by stays None (tombstone, not
  correction); retrieval skips item via normal query; item still retrievable
  with include_history=True.
"""

from __future__ import annotations

import pathlib

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import MemoryItem, SensitiveField


def _make_item(item_id: str = "item-forget-001") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": "user works at Acme Corp"},
        source_event_id="evt-src-001",
        created_at="2026-05-14T10:00:00+00:00",
        last_confirmed_at="2026-05-14T10:00:00+00:00",
        confidence=0.9,
        salience=0.8,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-14T10:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value="user works at Acme Corp",
        ),
    )


def test_forget_sets_valid_to(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    store.forget("item-forget-001")

    item = store.get("item-forget-001")
    assert item is not None
    assert item.valid_to is not None


def test_forget_leaves_superseded_by_none(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    store.forget("item-forget-001")

    item = store.get("item-forget-001")
    assert item.superseded_by is None


def test_forget_tombstone_retained(tmp_path: pathlib.Path) -> None:
    ep_dir = tmp_path / "episodic"
    store = EpisodicMemoryStore(ep_dir)
    store.commit(_make_item())
    store.forget("item-forget-001")

    # File (tombstone) must still exist on disk
    assert (ep_dir / "item-forget-001.json").exists()


def test_forget_skips_normal_retrieval(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    store.forget("item-forget-001")

    results = store.retrieve("Acme")
    assert not any(r.item_id == "item-forget-001" for r in results)


def test_forget_visible_in_history(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    store.forget("item-forget-001")

    history = store.retrieve("Acme", top_k=10, include_history=True)
    assert any(r.item_id == "item-forget-001" for r in history)
