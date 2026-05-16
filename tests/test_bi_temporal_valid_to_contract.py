"""Bi-temporal valid_to contract — uniform across all 4 MemoryManager stores (issue #104).

Contract (locked in memory_manager.py MemoryManager docstring):
  Active iff valid_to is None OR valid_to > now_utc(), AND superseded_by is None.
  Default retrieve() excludes inactive items.
  retrieve(..., include_history=True) includes all items (active + invalidated).
"""

from __future__ import annotations

import pathlib

import pytest

from companion_harness.core_user_profile_store import CoreUserProfileStore
from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.memory_manager import MemoryManager
from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.semantic_relational_store import SemanticRelationalStore
from companion_harness.session_state_store import SessionStateStore


# ---------------------------------------------------------------------------
# Store factories
# ---------------------------------------------------------------------------

def _session_store(tmp_path: pathlib.Path) -> MemoryManager:
    return SessionStateStore()


def _core_store(tmp_path: pathlib.Path) -> MemoryManager:
    return CoreUserProfileStore(tmp_path / "profile.json")


def _episodic_store(tmp_path: pathlib.Path) -> MemoryManager:
    return EpisodicMemoryStore(tmp_path / "episodic")


def _semantic_store(tmp_path: pathlib.Path) -> MemoryManager:
    return SemanticRelationalStore(tmp_path / "semantic")


_ALL_STORES = [_session_store, _core_store, _episodic_store, _semantic_store]
_STORE_IDS = ["session", "core_profile", "episodic", "semantic_relational"]


# ---------------------------------------------------------------------------
# Item builders — one per store type (content shapes differ for semantic)
# ---------------------------------------------------------------------------

def _make_item(
    store_literal: str,
    item_id: str = "itm-001",
    valid_to: str | None = None,
    superseded_by: str | None = None,
    content: dict | None = None,
) -> MemoryItem:
    if store_literal == "semantic_relational":
        default_content: dict = {"subject": "alice", "predicate": "likes", "object": "coffee"}
    else:
        default_content = {"summary": "the quick brown fox"}
    return MemoryItem(
        item_id=item_id,
        store=store_literal,  # type: ignore[arg-type]
        content=content if content is not None else default_content,
        source_event_id="evt-src-001",
        created_at="2026-01-01T00:00:00+00:00",
        last_confirmed_at="2026-01-01T00:00:00+00:00",
        confidence=0.9,
        salience=0.8,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to=valid_to,
        superseded_by=superseded_by,
        user_visible_summary=SensitiveField(retention_policy_id="default", value="test"),
    )


_STORE_LITERAL = {
    _session_store: "session",
    _core_store: "core_profile",
    _episodic_store: "episodic",
    _semantic_store: "semantic_relational",
}


# ---------------------------------------------------------------------------
# test_protocol_contract_enforced: all 4 stores satisfy MemoryManager Protocol
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", _ALL_STORES, ids=_STORE_IDS)
def test_protocol_contract_enforced(tmp_path: pathlib.Path, factory) -> None:
    store = factory(tmp_path)
    assert isinstance(store, MemoryManager)


# ---------------------------------------------------------------------------
# Default retrieve() excludes past-valid_to items (per-store)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", _ALL_STORES, ids=_STORE_IDS)
def test_past_valid_to_excluded_by_default(tmp_path: pathlib.Path, factory) -> None:
    store = factory(tmp_path)
    literal = _STORE_LITERAL[factory]
    store.commit(_make_item(literal, item_id="itm-past", valid_to="2020-01-01T00:00:00+00:00"))
    results = store.retrieve("alice" if literal == "semantic_relational" else "fox")
    assert not any(r.item_id == "itm-past" for r in results)


# ---------------------------------------------------------------------------
# Default retrieve() excludes superseded items (per-store)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", _ALL_STORES, ids=_STORE_IDS)
def test_superseded_by_excluded_by_default(tmp_path: pathlib.Path, factory) -> None:
    store = factory(tmp_path)
    literal = _STORE_LITERAL[factory]
    query = "alice" if literal == "semantic_relational" else "fox"
    store.commit(_make_item(literal, item_id="itm-old", superseded_by="itm-new"))
    store.commit(_make_item(literal, item_id="itm-new"))
    results = store.retrieve(query)
    ids = [r.item_id for r in results]
    assert "itm-old" not in ids
    assert "itm-new" in ids


# ---------------------------------------------------------------------------
# Default retrieve() INCLUDES items with future valid_to (per-store)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", _ALL_STORES, ids=_STORE_IDS)
def test_future_valid_to_included_by_default(tmp_path: pathlib.Path, factory) -> None:
    store = factory(tmp_path)
    literal = _STORE_LITERAL[factory]
    query = "alice" if literal == "semantic_relational" else "fox"
    store.commit(_make_item(literal, item_id="itm-future", valid_to="2099-01-01T00:00:00+00:00"))
    results = store.retrieve(query)
    assert any(r.item_id == "itm-future" for r in results)


# ---------------------------------------------------------------------------
# include_history=True includes past-valid_to items (per-store)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", _ALL_STORES, ids=_STORE_IDS)
def test_include_history_returns_past_valid_to(tmp_path: pathlib.Path, factory) -> None:
    store = factory(tmp_path)
    literal = _STORE_LITERAL[factory]
    query = "alice" if literal == "semantic_relational" else "fox"
    store.commit(_make_item(literal, item_id="itm-hist", valid_to="2020-01-01T00:00:00+00:00"))
    results = store.retrieve(query, top_k=10, include_history=True)
    assert any(r.item_id == "itm-hist" for r in results)


# ---------------------------------------------------------------------------
# include_history=True includes superseded items (per-store)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("factory", _ALL_STORES, ids=_STORE_IDS)
def test_include_history_returns_superseded(tmp_path: pathlib.Path, factory) -> None:
    store = factory(tmp_path)
    literal = _STORE_LITERAL[factory]
    query = "alice" if literal == "semantic_relational" else "fox"
    store.commit(_make_item(literal, item_id="itm-sup", superseded_by="itm-leaf"))
    store.commit(_make_item(literal, item_id="itm-leaf"))
    results = store.retrieve(query, top_k=10, include_history=True)
    ids = [r.item_id for r in results]
    assert "itm-sup" in ids
    assert "itm-leaf" in ids


# ---------------------------------------------------------------------------
# test_superseded_by_chains_correctly — supersede() on episodic/semantic
# ---------------------------------------------------------------------------

def test_superseded_by_chains_correctly_episodic(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    old = _make_item("episodic", item_id="ep-v1", content={"summary": "v1 fact"})
    new = _make_item("episodic", item_id="ep-v2", content={"summary": "v2 fact"})
    store.commit(old)
    store.supersede("ep-v1", new)

    v1 = store.get("ep-v1")
    assert v1 is not None
    assert v1.valid_to is not None
    assert v1.superseded_by == "ep-v2"

    # default retrieve: only v2
    results = store.retrieve("fact")
    ids = [r.item_id for r in results]
    assert "ep-v1" not in ids
    assert "ep-v2" in ids

    # history retrieve: both
    history = store.retrieve("fact", top_k=10, include_history=True)
    hist_ids = [r.item_id for r in history]
    assert "ep-v1" in hist_ids
    assert "ep-v2" in hist_ids


def test_superseded_by_chains_correctly_semantic(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    old = _make_item(
        "semantic_relational", item_id="sr-v1",
        content={"subject": "alice", "predicate": "works_at", "object": "acme"},
    )
    new = _make_item(
        "semantic_relational", item_id="sr-v2",
        content={"subject": "alice", "predicate": "works_at", "object": "betacorp"},
    )
    store.commit(old)
    store.supersede("sr-v1", new)

    v1 = store.get("sr-v1")
    assert v1 is not None
    assert v1.valid_to is not None
    assert v1.superseded_by == "sr-v2"

    # default retrieve: only v2
    results = store.retrieve("alice")
    ids = [r.item_id for r in results]
    assert "sr-v1" not in ids
    assert "sr-v2" in ids

    # history retrieve: both
    history = store.retrieve("alice", top_k=10, include_history=True)
    hist_ids = [r.item_id for r in history]
    assert "sr-v1" in hist_ids
    assert "sr-v2" in hist_ids
