"""SessionStateStore unit tests (v0.1e Task 7).

Success criterion: session-scope reads/writes round-trip provenance fields
correctly; retrieval excludes expired and superseded items.
"""

from companion_harness.memory_manager import MemoryManager
from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.session_state_store import SessionStateStore


def _item(
    item_id: str = "item-1",
    content: dict | None = None,
    valid_to: str | None = None,
    superseded_by: str | None = None,
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="session",
        content=content if content is not None else {"key": "hello world"},
        source_event_id="evt-1",
        created_at="2026-01-01T00:00:00+00:00",
        last_confirmed_at="2026-01-01T00:00:00+00:00",
        confidence=0.9,
        salience=0.8,
        privacy_level="default",
        mutability="system_revisable",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to=valid_to,
        superseded_by=superseded_by,
        user_visible_summary=SensitiveField(retention_policy_id="default", value="test summary"),
    )


def test_commit_then_retrieve():
    store = SessionStateStore()
    store.commit(_item(content={"key": "hello world"}))
    results = store.retrieve("hello")
    assert len(results) == 1
    assert results[0].item_id == "item-1"


def test_satisfies_memory_manager_protocol():
    assert isinstance(SessionStateStore(), MemoryManager)


def test_valid_to_excludes_from_retrieval():
    store = SessionStateStore()
    # valid_to in the past
    store.commit(_item(content={"key": "expired item"}, valid_to="2020-01-01T00:00:00+00:00"))
    results = store.retrieve("expired")
    assert results == []


def test_superseded_by_excludes_from_retrieval():
    store = SessionStateStore()
    # "old" carries superseded_by pointing at its replacement "new"
    store.commit(_item(item_id="old", content={"key": "old fact"}, superseded_by="new"))
    store.commit(_item(item_id="new", content={"key": "new fact"}))
    # "old" excluded (superseded_by set); "new" is active
    results = store.retrieve("fact")
    assert len(results) == 1
    assert results[0].item_id == "new"


def test_provenance_round_trip():
    store = SessionStateStore()
    original = _item(
        item_id="prov-1",
        content={"fact": "user is left-handed"},
    )
    store.commit(original)
    results = store.retrieve("left-handed")
    assert len(results) == 1
    r = results[0]
    assert r.source_event_id == "evt-1"
    assert r.confidence == 0.9
    assert r.salience == 0.8
    assert r.valid_from == "2026-01-01T00:00:00+00:00"
    assert r.valid_to is None
    assert r.superseded_by is None
    assert r.user_visible_summary.value == "test summary"
