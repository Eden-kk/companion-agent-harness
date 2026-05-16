"""v0.1g Task 8 — recent_shared_moments wiring (projection over episodic_memory).

Success criterion: no isinstance(obj, MemoryManager) callsite returns False.

Tests:
- test_episodic_retrieve_shared_moments_returns_last_n
- test_session_state_returns_empty
- test_core_user_profile_returns_empty
- test_semantic_relational_returns_empty
- test_companion_state_recent_shared_moments_is_projection
- test_all_memory_manager_subclasses_implement_retrieve_shared_moments
"""

from __future__ import annotations

import dataclasses
import pathlib

import pytest

from companion_harness.core_user_profile_store import CoreUserProfileStore
from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.memory_manager import MemoryManager, MemoryManagerStub
from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.semantic_relational_store import SemanticRelationalStore
from companion_harness.session_state_store import SessionStateStore


def _make_item(item_id: str, created_at: str = "2026-05-15T00:00:00+00:00") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": f"moment {item_id}"},
        source_event_id="evt-001",
        created_at=created_at,
        last_confirmed_at=created_at,
        confidence=0.9,
        salience=0.8,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from=created_at,
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value=f"moment {item_id}",
        ),
    )


def test_episodic_retrieve_shared_moments_returns_last_n(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item("ep-001", "2026-05-13T00:00:00+00:00"))
    store.commit(_make_item("ep-002", "2026-05-14T00:00:00+00:00"))
    store.commit(_make_item("ep-003", "2026-05-15T00:00:00+00:00"))
    store.commit(_make_item("ep-004", "2026-05-16T00:00:00+00:00"))
    store.commit(_make_item("ep-005", "2026-05-17T00:00:00+00:00"))
    store.commit(_make_item("ep-006", "2026-05-18T00:00:00+00:00"))

    results = store.retrieve_shared_moments(n=5)
    assert len(results) == 5
    ids = [r.item_id for r in results]
    # Most recent first
    assert ids == ["ep-006", "ep-005", "ep-004", "ep-003", "ep-002"]


def test_episodic_retrieve_shared_moments_excludes_inactive(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item("ep-active", "2026-05-15T10:00:00+00:00"))
    store.commit(
        dataclasses.replace(
            _make_item("ep-forgotten", "2026-05-15T11:00:00+00:00"),
            valid_to="2020-01-01T00:00:00+00:00",
        )
    )
    store.commit(
        dataclasses.replace(
            _make_item("ep-superseded", "2026-05-15T12:00:00+00:00"),
            superseded_by="ep-other",
        )
    )
    results = store.retrieve_shared_moments(n=5)
    ids = [r.item_id for r in results]
    assert "ep-active" in ids
    assert "ep-forgotten" not in ids
    assert "ep-superseded" not in ids


def test_episodic_retrieve_shared_moments_fewer_than_n(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item("ep-001", "2026-05-15T00:00:00+00:00"))
    store.commit(_make_item("ep-002", "2026-05-16T00:00:00+00:00"))
    results = store.retrieve_shared_moments(n=5)
    assert len(results) == 2


def test_episodic_retrieve_shared_moments_empty_store(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    assert store.retrieve_shared_moments() == []


def test_session_state_returns_empty() -> None:
    store = SessionStateStore()
    assert store.retrieve_shared_moments() == []
    assert store.retrieve_shared_moments(n=3) == []


def test_core_user_profile_returns_empty(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    assert store.retrieve_shared_moments() == []
    assert store.retrieve_shared_moments(n=3) == []


def test_semantic_relational_returns_empty(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    assert store.retrieve_shared_moments() == []
    assert store.retrieve_shared_moments(n=3) == []


def test_companion_state_recent_shared_moments_is_projection(tmp_path: pathlib.Path) -> None:
    """Anchor 3: recent_shared_moments is computed on-demand; no cached snapshot."""
    store = EpisodicMemoryStore(tmp_path / "ep")

    # Before any commits, projection returns empty
    assert store.retrieve_shared_moments() == []

    # After a commit, projection reflects it immediately without re-instantiating
    store.commit(_make_item("ep-001", "2026-05-15T00:00:00+00:00"))
    results = store.retrieve_shared_moments()
    assert len(results) == 1
    assert results[0].item_id == "ep-001"

    # After a second commit, projection reflects it too
    store.commit(_make_item("ep-002", "2026-05-16T00:00:00+00:00"))
    results2 = store.retrieve_shared_moments()
    assert len(results2) == 2
    assert results2[0].item_id == "ep-002"  # most recent first


def test_all_memory_manager_subclasses_implement_retrieve_shared_moments(
    tmp_path: pathlib.Path,
) -> None:
    """No isinstance(obj, MemoryManager) callsite returns False."""
    stores = [
        EpisodicMemoryStore(tmp_path / "ep"),
        SessionStateStore(),
        CoreUserProfileStore(tmp_path / "profile.json"),
        SemanticRelationalStore(tmp_path / "sr"),
        MemoryManagerStub(),
    ]
    for store in stores:
        assert isinstance(store, MemoryManager), (
            f"{type(store).__name__} does not satisfy MemoryManager Protocol"
        )
