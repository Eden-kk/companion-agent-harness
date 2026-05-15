"""Task 16: correction contract test — "actually I don't work there anymore" (v0.1e).

Spec: docs/architecture-v0.1.md §Stage 4 lines 536-538.

Success criterion: supersede(old_id, new_item) →
  old item's valid_to set and superseded_by == new_item.item_id;
  retrieval excludes old (include_history=False) but returns both
  (include_history=True); get(old_id) still returns the superseded item.
"""

from __future__ import annotations

import pathlib

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import MemoryItem, SensitiveField


def _make_employer_item(item_id: str, employer: str, source_event_id: str) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"fact": "user_employer", "value": employer},
        source_event_id=source_event_id,
        created_at="2026-05-14T10:00:00+00:00",
        last_confirmed_at="2026-05-14T10:00:00+00:00",
        confidence=0.95,
        salience=0.8,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-14T10:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value=f"user employer is {employer}",
        ),
    )


def test_supersede_sets_old_valid_to(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_employer_item("fact-1", "Acme", "evt-001"))
    new_item = _make_employer_item("fact-2", "BetaCorp", "evt-002")
    store.supersede("fact-1", new_item)

    old = store.get("fact-1")
    assert old is not None
    assert old.valid_to is not None


def test_supersede_sets_superseded_by(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_employer_item("fact-1", "Acme", "evt-001"))
    new_item = _make_employer_item("fact-2", "BetaCorp", "evt-002")
    store.supersede("fact-1", new_item)

    old = store.get("fact-1")
    assert old.superseded_by == "fact-2"


def test_normal_retrieval_excludes_old_returns_new(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_employer_item("fact-1", "Acme", "evt-001"))
    new_item = _make_employer_item("fact-2", "BetaCorp", "evt-002")
    store.supersede("fact-1", new_item)

    results = store.retrieve("employer", top_k=10)
    ids = [r.item_id for r in results]
    assert "fact-1" not in ids
    assert "fact-2" in ids


def test_history_retrieval_returns_both(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_employer_item("fact-1", "Acme", "evt-001"))
    new_item = _make_employer_item("fact-2", "BetaCorp", "evt-002")
    store.supersede("fact-1", new_item)

    history = store.retrieve("employer", top_k=10, include_history=True)
    ids = [r.item_id for r in history]
    assert "fact-1" in ids
    assert "fact-2" in ids


def test_get_returns_superseded_item(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_employer_item("fact-1", "Acme", "evt-001"))
    new_item = _make_employer_item("fact-2", "BetaCorp", "evt-002")
    store.supersede("fact-1", new_item)

    old = store.get("fact-1")
    assert old is not None
    assert old.item_id == "fact-1"
    assert old.content["value"] == "Acme"
