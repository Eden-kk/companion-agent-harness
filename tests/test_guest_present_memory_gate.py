"""Task 20: guest_present privacy-mode store-level tests (v0.1e).

Spec: architecture-v0.1.md lines 828-831.
Success criterion: commit silently skipped; item NOT in store after attempt.
"""

from __future__ import annotations

import pathlib

from companion_harness.core_user_profile_store import CoreUserProfileStore
from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import MemoryItem, SensitiveField


def _item(item_id: str = "item-001", store: str = "episodic") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store=store,
        content={"summary": "user prefers tea"},
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
            value="user prefers tea",
        ),
    )


def test_episodic_guest_present_silently_skips(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_item(), privacy_mode="guest_present")  # must not raise
    assert store.retrieve("tea") == []


def test_episodic_guest_present_item_not_in_store(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_item(item_id="ep-g-001"), privacy_mode="guest_present")
    assert store.retrieve("ep-g-001") == []
    assert store.retrieve("tea") == []


def test_profile_guest_present_silently_skips(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(_item(store="core_user_profile"), privacy_mode="guest_present")  # must not raise
    assert store.retrieve("tea") == []


def test_profile_guest_present_item_not_in_store(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(_item(item_id="cup-g-001", store="core_user_profile"), privacy_mode="guest_present")
    assert store.retrieve("cup-g-001") == []
    assert store.retrieve("tea") == []
