"""Privacy-mode gates — unit tests (v0.1e Task 12).

Success criterion:
  test_no_camera_memory, test_guest_present_memory_gate,
  test_sensitive_conversation_retention, test_no_memory_mode,
  and test_local_only_mode_raises all pass (~57 tests total).
"""

from __future__ import annotations

import pathlib

import pytest

from companion_harness.core_user_profile_store import CoreUserProfileStore
from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import MemoryItem, SensitiveField
from companion_harness.semantic_relational_store import SemanticRelationalStore
from companion_harness.session_state_store import SessionStateStore


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_item(
    item_id: str = "item-001",
    store: str = "episodic",
    content: dict | None = None,
    retention_policy_id: str = "retrieval_audit_30d",
) -> MemoryItem:
    if content is None:
        content = {"summary": "user prefers tea"}
    return MemoryItem(
        item_id=item_id,
        store=store,
        content=content,
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


def _make_sr_item(
    item_id: str = "sr-001",
    retention_policy_id: str = "retrieval_audit_30d",
) -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="semantic_relational",
        content={"subject": "alice", "predicate": "likes", "object": "tea"},
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
            value="alice likes tea",
        ),
    )


# ---------------------------------------------------------------------------
# check_privacy_gate helper — standalone tests
# ---------------------------------------------------------------------------


def test_helper_normal_allows():
    check_privacy_gate(_make_item(), "normal")  # must not raise


def test_helper_child_present_allows():
    check_privacy_gate(_make_item(), "child_present")  # no-op in v0.1e


def test_helper_unknown_mode_allows():
    # unrecognised future modes should fall through as no-op
    check_privacy_gate(_make_item(), "some_future_mode")


def test_helper_no_memory_raises_skip():
    with pytest.raises(_SkipCommit):
        check_privacy_gate(_make_item(), "no_memory")


def test_helper_guest_present_raises_skip():
    with pytest.raises(_SkipCommit):
        check_privacy_gate(_make_item(), "guest_present")


def test_helper_no_camera_memory_visual_key_raises_value_error():
    item = _make_item(content={"frame_id": "f-001"})
    with pytest.raises(ValueError, match="no_camera_memory"):
        check_privacy_gate(item, "no_camera_memory")


def test_helper_no_camera_memory_image_ref_raises_value_error():
    item = _make_item(content={"image_ref": "img-abc"})
    with pytest.raises(ValueError, match="no_camera_memory"):
        check_privacy_gate(item, "no_camera_memory")


def test_helper_no_camera_memory_scene_frame_raises_value_error():
    item = _make_item(content={"scene_frame": "sf-xyz"})
    with pytest.raises(ValueError, match="no_camera_memory"):
        check_privacy_gate(item, "no_camera_memory")


def test_helper_no_camera_memory_non_visual_allows():
    item = _make_item(content={"summary": "user said hello"})
    check_privacy_gate(item, "no_camera_memory")  # must not raise


def test_helper_sensitive_conversation_long_term_raises_skip():
    item = _make_item(retention_policy_id="ep_default_30d")
    with pytest.raises(_SkipCommit, match="sensitive_conversation"):
        check_privacy_gate(item, "sensitive_conversation")


def test_helper_sensitive_conversation_audit_indefinite_raises_skip():
    item = _make_item(retention_policy_id="audit_indefinite")
    with pytest.raises(_SkipCommit, match="sensitive_conversation"):
        check_privacy_gate(item, "sensitive_conversation")


def test_helper_sensitive_conversation_core_profile_raises_skip():
    item = _make_item(retention_policy_id="core_profile_indefinite")
    with pytest.raises(_SkipCommit, match="sensitive_conversation"):
        check_privacy_gate(item, "sensitive_conversation")


def test_helper_sensitive_conversation_short_term_allows():
    item = _make_item(retention_policy_id="retrieval_audit_30d")
    check_privacy_gate(item, "sensitive_conversation")  # must not raise


def test_helper_local_only_raises_not_implemented():
    with pytest.raises(NotImplementedError, match="#96"):
        check_privacy_gate(_make_item(), "local_only")


# ---------------------------------------------------------------------------
# EpisodicMemoryStore — privacy gate integration
# ---------------------------------------------------------------------------


def test_episodic_normal_mode_commits(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item(), privacy_mode="normal")
    assert len(store.retrieve("tea")) == 1


def test_episodic_no_memory_blocks_commit(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item(), privacy_mode="no_memory")
    assert store.retrieve("tea") == []


def test_episodic_no_camera_memory_blocks_visual(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    with pytest.raises(ValueError, match="no_camera_memory"):
        store.commit(_make_item(content={"frame_id": "f-001"}), privacy_mode="no_camera_memory")


def test_episodic_no_camera_memory_allows_non_visual(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item(), privacy_mode="no_camera_memory")
    assert len(store.retrieve("tea")) == 1


def test_episodic_guest_present_skips_commit(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item(), privacy_mode="guest_present")
    assert store.retrieve("tea") == []


def test_episodic_sensitive_conversation_blocks_long_term(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(
        _make_item(item_id="ep-lt", retention_policy_id="ep_default_30d"),
        privacy_mode="sensitive_conversation",
    )
    assert store.retrieve("tea") == []


def test_episodic_sensitive_conversation_allows_short_term(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(
        _make_item(item_id="ep-st", retention_policy_id="retrieval_audit_30d"),
        privacy_mode="sensitive_conversation",
    )
    assert len(store.retrieve("tea")) == 1


def test_episodic_local_only_raises(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    with pytest.raises(NotImplementedError, match="#96"):
        store.commit(_make_item(), privacy_mode="local_only")


def test_episodic_child_present_commits_normally(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_item(), privacy_mode="child_present")
    assert len(store.retrieve("tea")) == 1


# ---------------------------------------------------------------------------
# SessionStateStore — privacy gate integration
# ---------------------------------------------------------------------------


def test_session_normal_mode_commits() -> None:
    store = SessionStateStore()
    store.commit(_make_item(), privacy_mode="normal")
    assert len(store.retrieve("tea")) == 1


def test_session_no_memory_blocks_commit() -> None:
    store = SessionStateStore()
    store.commit(_make_item(), privacy_mode="no_memory")
    assert store.retrieve("tea") == []


def test_session_no_camera_memory_blocks_visual() -> None:
    store = SessionStateStore()
    with pytest.raises(ValueError, match="no_camera_memory"):
        store.commit(_make_item(content={"frame_id": "f-001"}), privacy_mode="no_camera_memory")


def test_session_no_camera_memory_allows_non_visual() -> None:
    store = SessionStateStore()
    store.commit(_make_item(), privacy_mode="no_camera_memory")
    assert len(store.retrieve("tea")) == 1


def test_session_guest_present_skips_commit() -> None:
    store = SessionStateStore()
    store.commit(_make_item(), privacy_mode="guest_present")
    assert store.retrieve("tea") == []


def test_session_sensitive_conversation_blocks_long_term() -> None:
    store = SessionStateStore()
    store.commit(
        _make_item(item_id="ss-lt", retention_policy_id="ep_default_30d"),
        privacy_mode="sensitive_conversation",
    )
    assert store.retrieve("tea") == []


def test_session_sensitive_conversation_allows_short_term() -> None:
    store = SessionStateStore()
    store.commit(
        _make_item(item_id="ss-st", retention_policy_id="retrieval_audit_30d"),
        privacy_mode="sensitive_conversation",
    )
    assert len(store.retrieve("tea")) == 1


def test_session_local_only_raises() -> None:
    store = SessionStateStore()
    with pytest.raises(NotImplementedError, match="#96"):
        store.commit(_make_item(), privacy_mode="local_only")


def test_session_child_present_commits_normally() -> None:
    store = SessionStateStore()
    store.commit(_make_item(), privacy_mode="child_present")
    assert len(store.retrieve("tea")) == 1


# ---------------------------------------------------------------------------
# CoreUserProfileStore — privacy gate integration
# ---------------------------------------------------------------------------


def test_profile_normal_mode_commits(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(_make_item(), privacy_mode="normal")
    assert len(store.retrieve("tea")) == 1


def test_profile_no_memory_blocks_commit(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(_make_item(), privacy_mode="no_memory")
    assert store.retrieve("tea") == []


def test_profile_no_camera_memory_blocks_visual(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    with pytest.raises(ValueError, match="no_camera_memory"):
        store.commit(_make_item(content={"frame_id": "f-001"}), privacy_mode="no_camera_memory")


def test_profile_no_camera_memory_allows_non_visual(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(_make_item(), privacy_mode="no_camera_memory")
    assert len(store.retrieve("tea")) == 1


def test_profile_guest_present_skips_commit(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(_make_item(), privacy_mode="guest_present")
    assert store.retrieve("tea") == []


def test_profile_sensitive_conversation_blocks_long_term(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(
        _make_item(item_id="cup-lt", retention_policy_id="core_profile_indefinite"),
        privacy_mode="sensitive_conversation",
    )
    assert store.retrieve("tea") == []


def test_profile_sensitive_conversation_allows_short_term(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(
        _make_item(item_id="cup-st", retention_policy_id="retrieval_audit_30d"),
        privacy_mode="sensitive_conversation",
    )
    assert len(store.retrieve("tea")) == 1


def test_profile_local_only_raises(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    with pytest.raises(NotImplementedError, match="#96"):
        store.commit(_make_item(), privacy_mode="local_only")


def test_profile_child_present_commits_normally(tmp_path: pathlib.Path) -> None:
    store = CoreUserProfileStore(tmp_path / "profile.json")
    store.commit(_make_item(), privacy_mode="child_present")
    assert len(store.retrieve("tea")) == 1


# ---------------------------------------------------------------------------
# SemanticRelationalStore — privacy gate integration
# ---------------------------------------------------------------------------


def test_semantic_normal_mode_commits(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    store.commit(_make_sr_item(), privacy_mode="normal")
    assert len(store.retrieve("alice")) == 1


def test_semantic_no_memory_blocks_commit(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    store.commit(_make_sr_item(), privacy_mode="no_memory")
    assert store.retrieve("alice") == []


def test_semantic_no_camera_memory_blocks_visual(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    item = MemoryItem(
        item_id="sr-vis",
        store="semantic_relational",
        content={
            "subject": "alice",
            "predicate": "seen_at",
            "object": "park",
            "frame_id": "f-001",
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
            value="alice seen at park",
        ),
    )
    with pytest.raises(ValueError, match="no_camera_memory"):
        store.commit(item, privacy_mode="no_camera_memory")


def test_semantic_no_camera_memory_allows_non_visual(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    store.commit(_make_sr_item(), privacy_mode="no_camera_memory")
    assert len(store.retrieve("alice")) == 1


def test_semantic_guest_present_skips_commit(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    store.commit(_make_sr_item(), privacy_mode="guest_present")
    assert store.retrieve("alice") == []


def test_semantic_sensitive_conversation_blocks_long_term(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    store.commit(_make_sr_item(retention_policy_id="ep_default_30d"), privacy_mode="sensitive_conversation")
    assert store.retrieve("alice") == []


def test_semantic_sensitive_conversation_allows_short_term(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    store.commit(_make_sr_item(retention_policy_id="retrieval_audit_30d"), privacy_mode="sensitive_conversation")
    assert len(store.retrieve("alice")) == 1


def test_semantic_local_only_raises(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    with pytest.raises(NotImplementedError, match="#96"):
        store.commit(_make_sr_item(), privacy_mode="local_only")


def test_semantic_child_present_commits_normally(tmp_path: pathlib.Path) -> None:
    store = SemanticRelationalStore(tmp_path / "sr")
    store.commit(_make_sr_item(), privacy_mode="child_present")
    assert len(store.retrieve("alice")) == 1


# ---------------------------------------------------------------------------
# Protocol compliance — privacy_mode param accepted without breaking isinstance
# ---------------------------------------------------------------------------


def test_episodic_satisfies_memory_manager_protocol(tmp_path: pathlib.Path) -> None:
    from companion_harness.memory_manager import MemoryManager
    store = EpisodicMemoryStore(tmp_path / "ep")
    assert isinstance(store, MemoryManager)


def test_session_satisfies_memory_manager_protocol() -> None:
    from companion_harness.memory_manager import MemoryManager
    assert isinstance(SessionStateStore(), MemoryManager)


def test_profile_satisfies_memory_manager_protocol(tmp_path: pathlib.Path) -> None:
    from companion_harness.memory_manager import MemoryManager
    assert isinstance(CoreUserProfileStore(tmp_path / "profile.json"), MemoryManager)


def test_semantic_satisfies_memory_manager_protocol(tmp_path: pathlib.Path) -> None:
    from companion_harness.memory_manager import MemoryManager
    assert isinstance(SemanticRelationalStore(tmp_path / "sr"), MemoryManager)
