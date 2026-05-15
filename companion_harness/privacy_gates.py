"""Privacy-mode gate for MemoryManager.commit() (v0.1e Task 12).

check_privacy_gate() is called by each store's commit() with the active privacy_mode.
It returns silently if the commit is permitted, raises _SkipCommit if the commit
should be silently dropped, or raises NotImplementedError for local_only (hard-fail).

Spec reference: docs/architecture-v0.1.md §Part 7 lines 805-836.
"""

from __future__ import annotations

from companion_harness.schemas import MemoryItem

__all__ = ["check_privacy_gate"]

_LONG_TERM_RETENTION_IDS = frozenset(
    {"ep_default_30d", "audit_indefinite", "core_profile_indefinite"}
)

_VISUAL_CONTENT_KEYS = frozenset({"frame_id", "image_ref", "scene_frame"})


class _SkipCommit(Exception):
    """Signal that a commit should be silently dropped (not an error)."""


def check_privacy_gate(item: MemoryItem, privacy_mode: str) -> None:
    """Apply v0.1e privacy gate to a commit attempt.

    Returns silently if the commit is permitted.
    Raises _SkipCommit if the commit should be dropped (store catches and skips).
    Raises NotImplementedError for local_only (hard-fail per roadmap Task 12 / issue #96).
    Raises ValueError for no_camera_memory with visual-derived content.
    """
    if privacy_mode == "local_only":
        raise NotImplementedError(
            "local_only mode is not supported in v0.1e; see issue #96 "
            "for the v0.1f adapter-routing work"
        )
    if privacy_mode == "no_memory":
        raise _SkipCommit("no_memory mode blocks all durable writes")
    if privacy_mode == "no_camera_memory":
        if any(k in item.content for k in _VISUAL_CONTENT_KEYS):
            raise ValueError(
                f"no_camera_memory mode blocks visual-derived memory writes "
                f"(found visual key in content: {_VISUAL_CONTENT_KEYS & item.content.keys()})"
            )
    if privacy_mode == "guest_present":
        raise _SkipCommit("guest_present pauses durable writes (defense-in-depth)")
    if privacy_mode == "sensitive_conversation":
        if item.user_visible_summary.retention_policy_id in _LONG_TERM_RETENTION_IDS:
            raise _SkipCommit(
                f"sensitive_conversation blocks long-term retention "
                f"(retention_policy_id={item.user_visible_summary.retention_policy_id!r})"
            )
    # normal, child_present → no-op
