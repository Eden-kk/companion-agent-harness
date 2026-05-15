"""SleepTimeAgent — async memory-commit adapter (v0.1e Task 10).

Subscribes to memory_write_candidate events via EventLogger.subscribe(),
finalizes provenance fields on a MemoryItem, and commits it to the
appropriate store via MemoryManager.commit().

SC-1: memory-commit only (v0.1e). State-schema mutation deferred to v0.1f+.
Task 11 wires the foreground emit side (_read_payload shim replaced then).
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from companion_harness.schemas import Event, MemoryItem, SensitiveField

if TYPE_CHECKING:
    from companion_harness.event_logger import EventLogger
    from companion_harness.memory_manager import MemoryManager

__all__ = ["SleepTimeAgent"]

CONFIDENCE_DEFAULT = 0.8
SALIENCE_DEFAULT = 0.5
SUMMARY_TRUNCATE_CHARS = 80


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_event(
    event_type: str,
    caused_by: list[str],
    seq: int,
) -> Event:
    now_ms = int(time.monotonic() * 1000)
    return Event(
        event_id=f"sta-{event_type}-{now_ms}-{seq}",
        session_id="",
        schema_version="0.1",
        seq_no=seq,
        event_type=event_type,
        timestamp_mono_ms=now_ms,
        timestamp_wall=_now_utc(),
        source="sleep_time_agent",
        caused_by=caused_by,
        payload_hash="",
        payload_ref=None,
        payload_kind="memory_op",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="commit_audit_30d",
    )


class SleepTimeAgent:
    """Async memory-mutator (v0.1e scope: memory commits only).

    Subscribes to memory_write_candidate events (Task 11 emits), finalizes
    provenance fields (confidence, salience, user_visible_summary), commits
    a MemoryItem to the appropriate store via MemoryManager.commit().

    SC-1 scope: memory commits only (v0.1e). State-schema mutation deferred to v0.1f+.
    """

    def __init__(
        self,
        stores: dict[str, "MemoryManager"],
        event_logger: "EventLogger",
        *,
        clock: Callable[[], str] = _now_utc,
        item_id_factory: Callable[[], str] = lambda: f"item-{uuid.uuid4().hex[:12]}",
    ) -> None:
        self._stores = stores
        self._event_logger = event_logger
        self._clock = clock
        self._item_id_factory = item_id_factory
        self._started = False
        self._seq = 0

    async def start(self) -> None:
        if self._started:
            return
        self._event_logger.subscribe(self._on_event)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._event_logger.unsubscribe(self._on_event)
        self._started = False

    async def _on_event(self, event: Event) -> None:
        if event.event_type != "memory_write_candidate":
            return

        payload = self._read_payload(event)

        if payload.get("privacy_mode") == "guest_present":
            self._seq += 1
            self._event_logger.log(
                _make_event("memory_commit_skipped", [event.event_id], self._seq)
            )
            return

        item = self._finalize_provenance(payload)

        store = self._stores.get(payload["store"])
        if store is None:
            sys.stderr.write(
                f"[SleepTimeAgent] unknown/unwired store: {payload['store']}\n"
            )
            return

        store.commit(item)

        self._seq += 1
        self._event_logger.log(
            _make_event("memory_commit_completed", [event.event_id], self._seq)
        )

    def _read_payload(self, event: Event) -> dict:
        # TODO(Task 11): replace with production payload-access path
        return getattr(event, "payload_dict", {})

    def _finalize_provenance(self, payload: dict) -> MemoryItem:
        now = self._clock()
        item_id = payload.get("item_id") or self._item_id_factory()
        content = payload["content"]
        content_summary = json.dumps(content)[:SUMMARY_TRUNCATE_CHARS]
        return MemoryItem(
            item_id=item_id,
            store=payload["store"],
            content=content,
            source_event_id=payload["source_event_id"],
            created_at=now,
            last_confirmed_at=now,
            confidence=payload.get("confidence", CONFIDENCE_DEFAULT),
            salience=payload.get("salience", SALIENCE_DEFAULT),
            privacy_level=payload["privacy_level"],
            mutability=payload["mutability"],
            valid_from=now,
            valid_to=None,
            superseded_by=None,
            user_visible_summary=SensitiveField(
                retention_policy_id=payload["retention_policy_id"],
                value=content_summary,
                sensitivity=payload.get("sensitivity", "safe"),
                source_event_ids=[payload["source_event_id"]],
            ),
        )
