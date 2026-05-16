"""Task 19: no_camera_memory privacy-mode store-level tests (v0.1e).

Spec: architecture-v0.1.md lines 823-827.
Success criterion: visual-content commits return SKIPPED_CAMERA; non-visual commits succeed.
"""

from __future__ import annotations

import pathlib

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.memory_manager import CommitResult
from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.semantic_relational_store import SemanticRelationalStore


def _visual_item(store: str = "episodic") -> MemoryItem:
    return MemoryItem(
        item_id="item-vis-001",
        store=store,
        content={"frame_id": "f-001"},
        source_event_id="evt-001",
        created_at="2026-05-15T00:00:00+00:00",
        last_confirmed_at="2026-05-15T00:00:00+00:00",
        confidence=0.9,
        salience=0.7,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-15T00:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="retrieval_audit_30d",
            value="frame captured",
        ),
    )


def _nonvisual_item(store: str = "episodic") -> MemoryItem:
    return MemoryItem(
        item_id="item-txt-001",
        store=store,
        content={"summary": "user prefers tea"},
        source_event_id="evt-002",
        created_at="2026-05-15T00:00:00+00:00",
        last_confirmed_at="2026-05-15T00:00:00+00:00",
        confidence=0.9,
        salience=0.7,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-15T00:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="retrieval_audit_30d",
            value="user prefers tea",
        ),
    )


def test_episodic_visual_returns_skipped_camera(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    result = store.commit(_visual_item(), privacy_mode="no_camera_memory")
    assert result == CommitResult.SKIPPED_CAMERA
    assert store.retrieve("") == []


def test_episodic_nonvisual_succeeds(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_nonvisual_item(), privacy_mode="no_camera_memory")
    assert len(store.retrieve("tea")) == 1


def test_semantic_visual_returns_skipped_camera(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    item = MemoryItem(
        item_id="sr-vis-001",
        store="semantic_relational",
        content={"subject": "alice", "predicate": "seen_at", "object": "park", "frame_id": "f-001"},
        source_event_id="evt-001",
        created_at="2026-05-15T00:00:00+00:00",
        last_confirmed_at="2026-05-15T00:00:00+00:00",
        confidence=0.9,
        salience=0.7,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-15T00:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="retrieval_audit_30d",
            value="alice seen at park",
        ),
    )
    result = store.commit(item, privacy_mode="no_camera_memory")
    assert result == CommitResult.SKIPPED_CAMERA
    assert store.retrieve("alice") == []


def test_semantic_nonvisual_succeeds(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    item = MemoryItem(
        item_id="sr-txt-001",
        store="semantic_relational",
        content={"subject": "alice", "predicate": "likes", "object": "tea"},
        source_event_id="evt-002",
        created_at="2026-05-15T00:00:00+00:00",
        last_confirmed_at="2026-05-15T00:00:00+00:00",
        confidence=0.9,
        salience=0.7,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-15T00:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="retrieval_audit_30d",
            value="alice likes tea",
        ),
    )
    store.commit(item, privacy_mode="no_camera_memory")
    assert len(store.retrieve("alice")) == 1
