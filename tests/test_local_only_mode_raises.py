"""Task 23: local_only privacy mode — contract test (v0.1e).

Spec: docs/architecture-v0.1.md §Part 7 lines 811-818 + Path 1 resolution.

Success criterion: privacy_mode="local_only" → MemoryManager.commit() raises
  NotImplementedError with "#96" in message; action-triggered (only fires on
  commit attempt); no side effects before raise.
"""

from __future__ import annotations

import pathlib

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import MemoryItem, SensitiveField


def _make_item(item_id: str = "item-lo-001") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": "user runs local_only session"},
        source_event_id="evt-src-lo-001",
        created_at="2026-05-14T10:00:00+00:00",
        last_confirmed_at="2026-05-14T10:00:00+00:00",
        confidence=0.8,
        salience=0.5,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-14T10:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value="local_only session",
        ),
    )


def test_local_only_commit_raises_not_implemented(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    with pytest.raises(NotImplementedError):
        store.commit(_make_item(), privacy_mode="local_only")


def test_local_only_error_message_contains_issue_96(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    with pytest.raises(NotImplementedError, match="#96"):
        store.commit(_make_item(), privacy_mode="local_only")


def test_local_only_no_file_written_before_raise(tmp_path: pathlib.Path) -> None:
    ep_dir = tmp_path / "episodic"
    store = EpisodicMemoryStore(ep_dir)
    with pytest.raises(NotImplementedError):
        store.commit(_make_item(), privacy_mode="local_only")
    # No item file should exist
    assert not (ep_dir / "item-lo-001.json").exists()


def test_local_only_only_on_commit_attempt(tmp_path: pathlib.Path) -> None:
    """Constructing the store or creating an item does not raise."""
    ep_dir = tmp_path / "episodic"
    store = EpisodicMemoryStore(ep_dir)  # no raise
    item = _make_item()                   # no raise
    # Only the commit call triggers the gate
    with pytest.raises(NotImplementedError):
        store.commit(item, privacy_mode="local_only")
