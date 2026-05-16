"""Tests for session_state_store.forget() tombstone semantics (closes #105).

Success criterion: forget() sets valid_to (tombstone) instead of removing the
item; item is excluded from retrieve() but retrievable via get(); audit event
emitted when a logger is provided; idempotent on already-forgotten items.
"""

from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.session_state_store import SessionStateStore


def _item(
    item_id: str = "item-1",
    content: dict | None = None,
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="session",
        content=content if content is not None else {"key": "hello world"},
        source_event_id="evt-src-1",
        created_at="2026-01-01T00:00:00+00:00",
        last_confirmed_at="2026-01-01T00:00:00+00:00",
        confidence=0.9,
        salience=0.8,
        privacy_level="default",
        mutability="system_revisable",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(retention_policy_id="default", value="test summary"),
    )


def test_forget_sets_valid_to_not_removes():
    store = SessionStateStore()
    store.commit(_item())
    store.forget("item-1")
    # item still in store — not removed
    assert store.get("item-1") is not None
    # valid_to is now set (tombstoned)
    assert store.get("item-1").valid_to is not None
    # superseded_by unset (forget ≠ supersession)
    assert store.get("item-1").superseded_by is None


def test_default_retrieve_excludes_forgotten_items():
    store = SessionStateStore()
    store.commit(_item(content={"key": "unique phrase"}))
    store.forget("item-1")
    results = store.retrieve("unique phrase")
    assert results == []


def test_audit_retrieve_includes_forgotten_items():
    store = SessionStateStore()
    store.commit(_item())
    store.forget("item-1")
    # get() returns tombstone regardless of valid_to
    tombstone = store.get("item-1")
    assert tombstone is not None
    assert tombstone.item_id == "item-1"
    assert tombstone.valid_to is not None


def test_forget_emits_audit_tombstone_write():
    logged: list = []

    class _FakeLogger:
        def log(self, event) -> None:
            logged.append(event)

    store = SessionStateStore(logger=_FakeLogger())  # type: ignore[arg-type]
    store.commit(_item())
    store.forget("item-1")

    assert len(logged) == 1
    evt = logged[0]
    assert evt.event_type == "audit_tombstone_write"
    assert evt.source == "session_state_store"
    assert evt.payload_inline == {"item_id": "item-1"}
    assert evt.sensitivity == "safe"
    assert evt.retention_policy_id == "audit_indefinite"


def test_forget_idempotent():
    """Forgetting an already-forgotten item is a no-op; no duplicate event."""
    logged: list = []

    class _FakeLogger:
        def log(self, event) -> None:
            logged.append(event)

    store = SessionStateStore(logger=_FakeLogger())  # type: ignore[arg-type]
    store.commit(_item())
    store.forget("item-1")
    first_valid_to = store.get("item-1").valid_to

    store.forget("item-1")  # second call — no-op
    assert store.get("item-1").valid_to == first_valid_to  # unchanged
    assert len(logged) == 1  # no duplicate event
