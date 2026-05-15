"""Tests for EpisodicMemoryStore (v0.1e Task 5).

Success criterion: 20 tests pass; bi-temporal contract satisfied.
"""

import pathlib
import time

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.memory_manager import MemoryManager
from companion_harness.schemas import MemoryItem, SensitiveField


def _make_item(item_id: str = "ep-001", **overrides) -> MemoryItem:
    defaults = dict(
        item_id=item_id,
        store="episodic",
        content={"summary": "user visited Seattle"},
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
            value="user visited Seattle",
        ),
    )
    defaults.update(overrides)
    return MemoryItem(**defaults)


def test_commit_then_retrieve_same_process(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    results = store.retrieve("Seattle")
    assert len(results) == 1
    assert results[0].item_id == "ep-001"


def test_satisfies_memory_manager_protocol(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    assert isinstance(store, MemoryManager)


def test_persists_across_instances(tmp_path: pathlib.Path) -> None:
    ep_dir = tmp_path / "episodic"
    EpisodicMemoryStore(ep_dir).commit(_make_item())
    results = EpisodicMemoryStore(ep_dir).retrieve("Seattle")
    assert len(results) == 1
    assert results[0].item_id == "ep-001"


def test_atomic_write_no_partial_state(tmp_path: pathlib.Path) -> None:
    ep_dir = tmp_path / "episodic"
    store = EpisodicMemoryStore(ep_dir)
    store.commit(_make_item())
    assert list(ep_dir.glob("*.tmp")) == []


def test_mkdir_on_construct(tmp_path: pathlib.Path) -> None:
    ep_dir = tmp_path / "nested" / "episodic"
    assert not ep_dir.exists()
    EpisodicMemoryStore(ep_dir)
    assert ep_dir.is_dir()


def test_provenance_round_trip(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    item = _make_item(
        item_id="ep-prov",
        content={"summary": "user met Bob at conference"},
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
            value="user met Bob",
            sensitivity="sensitive",
        ),
    )
    store.commit(item)
    results = store.retrieve("Bob")
    assert len(results) == 1
    r = results[0]
    assert r.item_id == "ep-prov"
    assert r.source_event_id == "evt-prov-99"
    assert r.created_at == "2026-01-10T09:00:00+00:00"
    assert r.confidence == pytest.approx(0.88)
    assert r.salience == pytest.approx(0.65)
    assert r.valid_to is None
    assert r.superseded_by is None
    assert r.user_visible_summary.retention_policy_id == "audit_indefinite"
    assert r.user_visible_summary.value == "user met Bob"
    assert r.user_visible_summary.sensitivity == "sensitive"


def test_future_valid_to_remains_active(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item(item_id="ep-future", valid_to="2099-01-01T00:00:00+00:00"))
    results = store.retrieve("Seattle")
    assert any(r.item_id == "ep-future" for r in results)


def test_past_valid_to_excluded(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item(item_id="ep-past", valid_to="2020-01-01T00:00:00+00:00"))
    results = store.retrieve("Seattle")
    assert not any(r.item_id == "ep-past" for r in results)


def test_superseded_by_excluded(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item(item_id="ep-old", superseded_by="ep-new"))
    store.commit(_make_item(item_id="ep-new"))
    results = store.retrieve("Seattle")
    ids = [r.item_id for r in results]
    assert "ep-old" not in ids
    assert "ep-new" in ids


def test_supersede_marks_old_and_inserts_new(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    old = _make_item(item_id="ep-old", content={"summary": "user in Seattle"})
    new = _make_item(item_id="ep-new", content={"summary": "user in Portland"})
    store.commit(old)
    store.supersede("ep-old", new)

    old_loaded = store.get("ep-old")
    assert old_loaded.valid_to is not None
    assert old_loaded.superseded_by == "ep-new"

    results = store.retrieve("Portland")
    assert any(r.item_id == "ep-new" for r in results)
    results2 = store.retrieve("Seattle")
    assert not any(r.item_id == "ep-old" for r in results2)


def test_supersede_preserves_old_fields(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    old = _make_item(
        item_id="ep-old",
        source_event_id="evt-original",
        confidence=0.77,
        content={"summary": "user visited Tokyo"},
    )
    new = _make_item(item_id="ep-new")
    store.commit(old)
    store.supersede("ep-old", new)

    old_loaded = store.get("ep-old")
    assert old_loaded.source_event_id == "evt-original"
    assert old_loaded.confidence == pytest.approx(0.77)
    assert old_loaded.content == {"summary": "user visited Tokyo"}
    assert old_loaded.superseded_by == "ep-new"
    assert old_loaded.valid_to is not None


def test_forget_sets_valid_to_only(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item(item_id="ep-forget"))
    store.forget("ep-forget")

    item = store.get("ep-forget")
    assert item.valid_to is not None
    assert item.superseded_by is None

    results = store.retrieve("Seattle")
    assert not any(r.item_id == "ep-forget" for r in results)


def test_hard_delete_removes_file(tmp_path: pathlib.Path) -> None:
    ep_dir = tmp_path / "episodic"
    store = EpisodicMemoryStore(ep_dir)
    store.commit(_make_item(item_id="ep-del"))
    assert (ep_dir / "ep-del.json").exists()
    store.hard_delete("ep-del")
    assert not (ep_dir / "ep-del.json").exists()


def test_retrieve_top_k_truncation(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    for i in range(7):
        store.commit(_make_item(item_id=f"ep-{i:03d}"))
    results = store.retrieve("Seattle", top_k=5)
    assert len(results) == 5


def test_retrieve_include_history(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    active = _make_item(item_id="ep-active", content={"summary": "history test active"})
    forgotten = _make_item(
        item_id="ep-forgotten",
        content={"summary": "history test forgotten"},
        valid_to="2020-01-01T00:00:00+00:00",
    )
    superseded = _make_item(
        item_id="ep-superseded",
        content={"summary": "history test superseded"},
        superseded_by="ep-other",
    )
    store.commit(active)
    store.commit(forgotten)
    store.commit(superseded)

    default_results = store.retrieve("history test")
    default_ids = [r.item_id for r in default_results]
    assert "ep-active" in default_ids
    assert "ep-forgotten" not in default_ids
    assert "ep-superseded" not in default_ids

    all_results = store.retrieve("history test", top_k=10, include_history=True)
    all_ids = [r.item_id for r in all_results]
    assert "ep-active" in all_ids
    assert "ep-forgotten" in all_ids
    assert "ep-superseded" in all_ids


def test_get_returns_active_and_inactive(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item(item_id="ep-sup", superseded_by="ep-leaf"))
    item = store.get("ep-sup")
    assert item is not None
    assert item.item_id == "ep-sup"


def test_get_returns_none_after_hard_delete(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item(item_id="ep-gone"))
    store.hard_delete("ep-gone")
    assert store.get("ep-gone") is None


def test_supersede_on_already_superseded_raises(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    old = _make_item(item_id="ep-old")
    leaf = _make_item(item_id="ep-leaf")
    store.commit(old)
    store.supersede("ep-old", leaf)

    another_new = _make_item(item_id="ep-another")
    with pytest.raises(ValueError) as exc_info:
        store.supersede("ep-old", another_new)
    msg = str(exc_info.value)
    assert "ep-old" in msg
    assert "ep-leaf" in msg


def test_forget_idempotent_on_already_inactive(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")

    # (a) forget-then-forget: original tombstone timestamp preserved
    store.commit(_make_item(item_id="ep-ff"))
    store.forget("ep-ff")
    first_valid_to = store.get("ep-ff").valid_to
    time.sleep(0.01)
    store.forget("ep-ff")
    assert store.get("ep-ff").valid_to == first_valid_to

    # (b) supersede-then-forget: forget on superseded item is a no-op
    old = _make_item(item_id="ep-sf-old")
    new = _make_item(item_id="ep-sf-new")
    store.commit(old)
    store.supersede("ep-sf-old", new)
    supersede_valid_to = store.get("ep-sf-old").valid_to
    time.sleep(0.01)
    store.forget("ep-sf-old")
    assert store.get("ep-sf-old").valid_to == supersede_valid_to


def test_retrieve_deterministic_top_k_order(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    # Commit in non-alphabetical order
    for item_id in ["ep-zzz", "ep-aaa", "ep-mmm", "ep-bbb", "ep-nnn"]:
        store.commit(_make_item(item_id=item_id))

    results = store.retrieve("Seattle", top_k=3)
    assert len(results) == 3
    ids = [r.item_id for r in results]
    assert ids == sorted(ids)
    assert ids == ["ep-aaa", "ep-bbb", "ep-mmm"]
