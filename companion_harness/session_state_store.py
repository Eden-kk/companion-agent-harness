"""SessionStateStore — in-memory, session-scoped MemoryManager (v0.1e Task 7).

No persistence: state dies at process end. Bi-temporal filtering on retrieve:
items with valid_to set to a past timestamp or superseded_by non-None are
excluded. Full chain traversal deferred to episodic_memory (Task 5).
"""

from __future__ import annotations

import dataclasses
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from companion_harness.memory_manager import CommitResult
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import Event, MemoryItem

if TYPE_CHECKING:
    from companion_harness.event_logger import EventLogger

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

    def __init__(self, logger: EventLogger | None = None) -> None:
        self._items: dict[str, MemoryItem] = {}
        self._logger = logger
        self._seq = 0

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> CommitResult:
        try:
            check_privacy_gate(item, privacy_mode)
        except _SkipCommit:
            return CommitResult.SKIPPED_PRIVACY
        except ValueError:
            return CommitResult.SKIPPED_CAMERA
        self._items[item.item_id] = item
        return CommitResult.COMMITTED

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        q = query.lower()
        hits = [
            item for item in self._items.values()
            if _is_active(item) and q in str(item.content).lower()
        ]
        return hits[:top_k]

    def forget(self, item_id: str) -> None:
        item = self._items.get(item_id)
        if item is None:
            return
        if item.valid_to is not None:
            return
        self._items[item_id] = dataclasses.replace(item, valid_to=_now_utc())
        if self._logger is not None:
            self._seq += 1
            now_ms = int(time.monotonic() * 1000)
            self._logger.log(Event(
                event_id=f"sss-tombstone-{uuid.uuid4().hex[:8]}",
                session_id="",
                schema_version="0.1",
                seq_no=self._seq,
                event_type="audit_tombstone_write",
                timestamp_mono_ms=now_ms,
                timestamp_wall=_now_utc(),
                source="session_state_store",
                caused_by=[item.source_event_id],
                payload_hash="",
                payload_ref=None,
                payload_kind="memory_op",
                subject_class="unknown",
                sensitivity="safe",
                retention_policy_id="audit_indefinite",
                payload_inline={"item_id": item_id},
            ))

    def hard_delete(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def get(self, item_id: str) -> MemoryItem | None:
        return self._items.get(item_id)

    def retrieve_shared_moments(self, n: int = 5) -> list[MemoryItem]:
        return []
