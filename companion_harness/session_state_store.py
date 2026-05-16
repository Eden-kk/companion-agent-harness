"""SessionStateStore — in-memory, session-scoped MemoryManager (v0.1e Task 7).

No persistence: state dies at process end. Bi-temporal filtering on retrieve:
items with valid_to set to a past timestamp or superseded_by non-None are
excluded. Full chain traversal deferred to episodic_memory (Task 5).
"""

from __future__ import annotations

from datetime import datetime, timezone

from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import MemoryItem

__all__ = ["SessionStateStore"]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_active(item: MemoryItem) -> bool:
    if item.superseded_by is not None:
        return False
    if item.valid_to is None:
        return True
    return item.valid_to > _now_utc()


class SessionStateStore:
    """In-memory store satisfying the MemoryManager Protocol."""

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        try:
            check_privacy_gate(item, privacy_mode)
        except _SkipCommit:
            return
        self._items[item.item_id] = item

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        q = query.lower()
        hits = [
            item for item in self._items.values()
            if _is_active(item) and q in str(item.content).lower()
        ]
        return hits[:top_k]

    def forget(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def hard_delete(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def retrieve_shared_moments(self, n: int = 5) -> list[MemoryItem]:
        return []
