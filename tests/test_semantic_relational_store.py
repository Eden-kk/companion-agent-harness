"""Tests for SemanticRelationalStore (v0.1e Task 9).

Success criterion: 22 tests pass; Anchor 1 shape contract + bi-temporal contract satisfied.
"""

import datetime
import pathlib
import time

import pytest

from companion_harness.memory_manager import MemoryManager
from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.semantic_relational_store import SemanticRelationalStore


def _make_item(item_id: str = "sr-001", **overrides) -> MemoryItem:
    defaults = dict(
        item_id=item_id,
        store="semantic_relational",
        content={
            "subject": "alice",
            "predicate": "likes",
            "object": "coffee",
        },
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
            value="alice likes coffee",
        ),
    )
    defaults.update(overrides)
    return MemoryItem(**defaults)


# ---------------------------------------------------------------------------
# Tests 1–14: direct Task 5 adaptations
# ---------------------------------------------------------------------------


def test_commit_then_retrieve_same_process(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item())
    results = store.retrieve("alice")
    assert len(results) == 1
    assert results[0].item_id == "sr-001"


def test_satisfies_memory_manager_protocol(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    assert isinstance(store, MemoryManager)


def test_persists_across_instances(tmp_path: pathlib.Path) -> None:
    sr_dir = tmp_path / "semantic"
    SemanticRelationalStore(sr_dir).commit(_make_item())
    results = SemanticRelationalStore(sr_dir).retrieve("alice")
    assert len(results) == 1
    assert results[0].item_id == "sr-001"


def test_atomic_write_no_partial_state(tmp_path: pathlib.Path) -> None:
    sr_dir = tmp_path / "semantic"
    store = SemanticRelationalStore(sr_dir)
    store.commit(_make_item())
    assert list(sr_dir.glob("*.tmp")) == []


def test_mkdir_on_construct(tmp_path: pathlib.Path) -> None:
    sr_dir = tmp_path / "nested" / "semantic"
    assert not sr_dir.exists()
    SemanticRelationalStore(sr_dir)
    assert sr_dir.is_dir()


def test_provenance_round_trip(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    item = _make_item(
        item_id="sr-prov",
        content={
            "subject": "alice",
            "predicate": "works_at",
            "object": "acme_corp",
            "qualifiers": {"as_of": "2024-06-01", "source": "explicit_remember"},
        },
        source_event_id="evt-prov-99",
        created_at="2026-01-10T09:00:00+00:00",
        last_confirmed_at="2026-02-01T12:00:00+00:00",
        confidence=0.88,
        salience=0.65,
        valid_from="2026-01-10T09:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="audit_indefinite",
            value="alice works at acme_corp",
            sensitivity="sensitive",
        ),
    )
    store.commit(item)
    results = store.retrieve("acme_corp")
    assert len(results) == 1
    r = results[0]
    assert r.item_id == "sr-prov"
    assert r.source_event_id == "evt-prov-99"
    assert r.created_at == "2026-01-10T09:00:00+00:00"
    assert r.confidence == pytest.approx(0.88)
    assert r.salience == pytest.approx(0.65)
    assert r.valid_to is None
    assert r.superseded_by is None
    assert r.content == {
        "subject": "alice",
        "predicate": "works_at",
        "object": "acme_corp",
        "qualifiers": {"as_of": "2024-06-01", "source": "explicit_remember"},
    }
    assert r.user_visible_summary.retention_policy_id == "audit_indefinite"
    assert r.user_visible_summary.value == "alice works at acme_corp"
    assert r.user_visible_summary.sensitivity == "sensitive"
    # Guard: user_visible_summary must be SensitiveField, not dict
    assert isinstance(r.user_visible_summary, SensitiveField)


def test_future_valid_to_remains_active(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(item_id="sr-future", valid_to="2099-01-01T00:00:00+00:00"))
    results = store.retrieve("alice")
    assert any(r.item_id == "sr-future" for r in results)


def test_past_valid_to_excluded(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(item_id="sr-past", valid_to="2020-01-01T00:00:00+00:00"))
    results = store.retrieve("alice")
    assert not any(r.item_id == "sr-past" for r in results)


def test_superseded_by_excluded(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(item_id="sr-old", superseded_by="sr-new"))
    store.commit(_make_item(item_id="sr-new"))
    results = store.retrieve("alice")
    ids = [r.item_id for r in results]
    assert "sr-old" not in ids
    assert "sr-new" in ids


def test_supersede_marks_old_and_inserts_new(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    old = _make_item(
        item_id="sr-old",
        content={"subject": "alice", "predicate": "likes", "object": "coffee"},
    )
    new = _make_item(
        item_id="sr-new",
        content={"subject": "alice", "predicate": "likes", "object": "tea"},
    )
    store.commit(old)
    store.supersede("sr-old", new)

    old_loaded = store.get("sr-old")
    assert old_loaded.valid_to is not None
    assert old_loaded.superseded_by == "sr-new"

    results = store.retrieve("tea")
    assert any(r.item_id == "sr-new" for r in results)
    results2 = store.retrieve("coffee")
    assert not any(r.item_id == "sr-old" for r in results2)


def test_forget_sets_valid_to_only(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(item_id="sr-forget"))
    store.forget("sr-forget")

    item = store.get("sr-forget")
    assert item.valid_to is not None
    assert item.superseded_by is None

    results = store.retrieve("alice")
    assert not any(r.item_id == "sr-forget" for r in results)


def test_hard_delete_removes_file(tmp_path: pathlib.Path) -> None:
    sr_dir = tmp_path / "semantic"
    store = SemanticRelationalStore(sr_dir)
    store.commit(_make_item(item_id="sr-del"))
    assert (sr_dir / "sr-del.json").exists()
    store.hard_delete("sr-del")
    assert not (sr_dir / "sr-del.json").exists()


def test_supersede_on_already_superseded_raises(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    old = _make_item(item_id="sr-old")
    leaf = _make_item(item_id="sr-leaf")
    store.commit(old)
    store.supersede("sr-old", leaf)

    another_new = _make_item(item_id="sr-another")
    with pytest.raises(ValueError) as exc_info:
        store.supersede("sr-old", another_new)
    msg = str(exc_info.value)
    assert "sr-old" in msg
    assert "sr-leaf" in msg


def test_forget_idempotent_on_already_inactive(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")

    # (a) forget-then-forget: original tombstone timestamp preserved
    store.commit(_make_item(item_id="sr-ff"))
    store.forget("sr-ff")
    first_valid_to = store.get("sr-ff").valid_to
    time.sleep(0.01)
    store.forget("sr-ff")
    assert store.get("sr-ff").valid_to == first_valid_to

    # (b) supersede-then-forget: forget on superseded item is a no-op
    old = _make_item(item_id="sr-sf-old")
    new = _make_item(item_id="sr-sf-new")
    store.commit(old)
    store.supersede("sr-sf-old", new)
    supersede_valid_to = store.get("sr-sf-old").valid_to
    time.sleep(0.01)
    store.forget("sr-sf-old")
    assert store.get("sr-sf-old").valid_to == supersede_valid_to


# ---------------------------------------------------------------------------
# Tests 15–22: relational-specific
# ---------------------------------------------------------------------------


def test_retrieve_by_subject_match(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(
        item_id="sr-a1",
        content={"subject": "alice", "predicate": "likes", "object": "coffee"},
    ))
    store.commit(_make_item(
        item_id="sr-a2",
        content={"subject": "alice", "predicate": "works_at", "object": "acme_corp"},
    ))
    store.commit(_make_item(
        item_id="sr-b1",
        content={"subject": "bob", "predicate": "likes", "object": "tea"},
    ))
    results = store.retrieve("alice", top_k=5)
    ids = [r.item_id for r in results]
    assert "sr-a1" in ids
    assert "sr-a2" in ids
    assert "sr-b1" not in ids


def test_retrieve_by_predicate_match(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(
        item_id="sr-p1",
        content={"subject": "alice", "predicate": "likes", "object": "coffee"},
    ))
    store.commit(_make_item(
        item_id="sr-p2",
        content={"subject": "bob", "predicate": "likes", "object": "tea"},
    ))
    store.commit(_make_item(
        item_id="sr-p3",
        content={"subject": "alice", "predicate": "works_at", "object": "acme_corp"},
    ))
    results = store.retrieve("likes", top_k=5)
    ids = [r.item_id for r in results]
    assert "sr-p1" in ids
    assert "sr-p2" in ids
    assert "sr-p3" not in ids


def test_retrieve_by_object_match(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(
        item_id="sr-o1",
        content={"subject": "alice", "predicate": "likes", "object": "coffee"},
    ))
    store.commit(_make_item(
        item_id="sr-o2",
        content={"subject": "bob", "predicate": "likes", "object": "tea"},
    ))
    store.commit(_make_item(
        item_id="sr-o3",
        content={"subject": "carol", "predicate": "drinks", "object": "coffee"},
    ))
    results = store.retrieve("coffee", top_k=5)
    ids = [r.item_id for r in results]
    assert "sr-o1" in ids
    assert "sr-o3" in ids
    assert "sr-o2" not in ids


def test_retrieve_does_not_match_schema_field_names(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    store.commit(_make_item(
        item_id="sr-fnames",
        content={
            "subject": "alice",
            "predicate": "likes",
            "object": "coffee",
            "qualifiers": {"as_of": "2024"},
        },
    ))
    # (a) "qualifiers" is a key in content, but not in the token bag
    assert store.retrieve("qualifiers", top_k=5) == []
    # (b) "subject" is a key in content, but not in the token bag
    assert store.retrieve("subject", top_k=5) == []


def test_retrieve_include_history_relational(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    active = _make_item(
        item_id="sr-active",
        content={"subject": "history", "predicate": "test", "object": "active"},
    )
    forgotten = _make_item(
        item_id="sr-forgotten",
        content={"subject": "history", "predicate": "test", "object": "forgotten"},
        valid_to="2020-01-01T00:00:00+00:00",
    )
    superseded = _make_item(
        item_id="sr-superseded",
        content={"subject": "history", "predicate": "test", "object": "superseded"},
        superseded_by="sr-other",
    )
    store.commit(active)
    store.commit(forgotten)
    store.commit(superseded)

    default_results = store.retrieve("history")
    default_ids = [r.item_id for r in default_results]
    assert "sr-active" in default_ids
    assert "sr-forgotten" not in default_ids
    assert "sr-superseded" not in default_ids

    all_results = store.retrieve("history", top_k=10, include_history=True)
    all_ids = [r.item_id for r in all_results]
    assert "sr-active" in all_ids
    assert "sr-forgotten" in all_ids
    assert "sr-superseded" in all_ids


def test_commit_rejects_invalid_shape(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")

    base_content = {"subject": "alice", "predicate": "likes", "object": "coffee"}

    def _bad(item_id: str, content: dict) -> MemoryItem:
        return _make_item(item_id=item_id, content=content)

    # missing subject
    c = {k: v for k, v in base_content.items() if k != "subject"}
    with pytest.raises(ValueError) as exc_info:
        store.commit(_bad("sr-bad-no-subject", c))
    assert "sr-bad-no-subject" in str(exc_info.value)
    assert "subject" in str(exc_info.value)

    # missing predicate
    c = {k: v for k, v in base_content.items() if k != "predicate"}
    with pytest.raises(ValueError) as exc_info:
        store.commit(_bad("sr-bad-no-predicate", c))
    assert "sr-bad-no-predicate" in str(exc_info.value)
    assert "predicate" in str(exc_info.value)

    # missing object
    c = {k: v for k, v in base_content.items() if k != "object"}
    with pytest.raises(ValueError) as exc_info:
        store.commit(_bad("sr-bad-no-object", c))
    assert "sr-bad-no-object" in str(exc_info.value)
    assert "object" in str(exc_info.value)

    # empty-string subject
    c = {**base_content, "subject": ""}
    with pytest.raises(ValueError) as exc_info:
        store.commit(_bad("sr-bad-empty-subject", c))
    assert "sr-bad-empty-subject" in str(exc_info.value)
    assert "subject" in str(exc_info.value)

    # non-string subject
    c = {**base_content, "subject": 123}
    with pytest.raises(ValueError) as exc_info:
        store.commit(_bad("sr-bad-nonstr-subject", c))
    assert "sr-bad-nonstr-subject" in str(exc_info.value)
    assert "subject" in str(exc_info.value)

    # qualifiers non-dict
    c = {**base_content, "qualifiers": "not_a_dict"}
    with pytest.raises(ValueError) as exc_info:
        store.commit(_bad("sr-bad-qualifiers-type", c))
    assert "sr-bad-qualifiers-type" in str(exc_info.value)
    assert "qualifiers" in str(exc_info.value)

    # qualifier value non-JSON-serializable (actual datetime object)
    c = {**base_content, "qualifiers": {"as_of": datetime.datetime.now()}}
    with pytest.raises(ValueError) as exc_info:
        store.commit(_bad("sr-bad-qualifier-value", c))
    assert "sr-bad-qualifier-value" in str(exc_info.value)
    assert "as_of" in str(exc_info.value)


def test_retrieve_deterministic_top_k_order(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    # Commit in non-alphabetical order
    for item_id in ["sr-zzz", "sr-aaa", "sr-mmm", "sr-bbb", "sr-nnn"]:
        store.commit(_make_item(item_id=item_id))

    results = store.retrieve("alice", top_k=3)
    assert len(results) == 3
    ids = [r.item_id for r in results]
    assert ids == sorted(ids)
    assert ids == ["sr-aaa", "sr-bbb", "sr-mmm"]


def test_supersede_validates_new_item(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "semantic")
    old = _make_item(item_id="sr-old-valid")
    store.commit(old)

    # new_item is missing "predicate"
    invalid_new = _make_item(
        item_id="sr-new-invalid",
        content={"subject": "alice", "object": "coffee"},
    )
    with pytest.raises(ValueError) as exc_info:
        store.supersede("sr-old-valid", invalid_new)
    assert "predicate" in str(exc_info.value)

    # old item's valid_to must still be None (validation ran BEFORE any write)
    old_after = store.get("sr-old-valid")
    assert old_after.valid_to is None
