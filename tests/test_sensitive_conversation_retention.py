"""Task 21: sensitive_conversation privacy-mode store-level tests (v0.1e).

Spec: architecture-v0.1.md lines 833-836.
Success criterion: long-term retention commits are skipped; short-term allowed.
"""

from __future__ import annotations

import pathlib

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.session_state_store import SessionStateStore


def _item(
    item_id: str = "item-001",
    store: str = "episodic",
    retention_policy_id: str = "retrieval_audit_30d",
) -> MemoryItem:
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
            retention_policy_id=retention_policy_id,
            value="user prefers tea",
        ),
    )


def test_episodic_long_term_retention_skipped(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_item(retention_policy_id="ep_default_30d"), privacy_mode="sensitive_conversation")
    assert store.retrieve("tea") == []


def test_episodic_short_term_retention_allowed(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_item(retention_policy_id="retrieval_audit_30d"), privacy_mode="sensitive_conversation")
    assert len(store.retrieve("tea")) == 1


def test_session_long_term_retention_skipped() -> None:
    store = SessionStateStore()
    store.commit(_item(store="session_state", retention_policy_id="ep_default_30d"), privacy_mode="sensitive_conversation")
    assert store.retrieve("tea") == []


def test_session_short_term_retention_allowed() -> None:
    store = SessionStateStore()
    store.commit(_item(store="session_state", retention_policy_id="retrieval_audit_30d"), privacy_mode="sensitive_conversation")
    assert len(store.retrieve("tea")) == 1
