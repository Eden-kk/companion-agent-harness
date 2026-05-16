"""SleepTimeAgent — async memory-commit adapter (v0.1e Task 10).

Subscribes to memory_write_candidate events via EventLogger.late_subscribe(),
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

from companion_harness.provenance_minicpm import ProvenanceComputer
from companion_harness.schemas import Event, MemoryItem, SensitiveField

from companion_harness.memory_manager import CommitResult

if TYPE_CHECKING:
    from companion_harness.event_logger import EventLogger
    from companion_harness.memory_manager import MemoryManager

__all__ = ["SleepTimeAgent"]

CONFIDENCE_DEFAULT = 0.8  # UNAVAILABLE: #188 — null path fallback (no LLM wired)
SALIENCE_DEFAULT = 0.5  # UNAVAILABLE: #188 — null path fallback (no LLM wired)
SUMMARY_TRUNCATE_CHARS = 80


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_event(
    event_type: str,
    caused_by: list[str],
    seq: int,
    reason: str | None = None,
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
        payload_ref=reason,
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
        payload_reader: Callable[[str], dict | None] | None = None,
        provenance_computer: "ProvenanceComputer | None" = None,
    ) -> None:
        self._stores = stores
        self._event_logger = event_logger
        self._clock = clock
        self._item_id_factory = item_id_factory
        self._payload_reader = payload_reader
        self._provenance_computer = provenance_computer
        self._started = False
        self._seq = 0

    async def start(self) -> None:
        if self._started:
            return
        self._event_logger.late_subscribe(self._on_event)
        self._started = True

    async def stop(self) -> None:
        if not self._started:
            return
        self._event_logger.unsubscribe(self._on_event)
        self._started = False

    async def _on_event(self, event: Event) -> None:
        if event.event_type == "memory_write_candidate":
            await self._handle_write_candidate(event)
            return
        if event.event_type == "explicit_forget":
            await self._handle_explicit_forget(event)
            return

    async def _handle_write_candidate(self, event: Event) -> None:
        payload = self._read_payload(event)
        item = self._finalize_provenance(payload)

        store = self._stores.get(payload["store"])
        if store is None:
            sys.stderr.write(
                f"[SleepTimeAgent] unknown/unwired store: {payload['store']}\n"
            )
            return

        try:
            result = store.commit(item, privacy_mode=payload.get("privacy_mode", "normal"))
        except Exception as exc:
            sys.stderr.write(f"[SleepTimeAgent] commit error: {exc}\n")
            return

        self._seq += 1
        if result == CommitResult.COMMITTED:
            self._event_logger.log(
                _make_event("memory_commit_completed", [event.event_id], self._seq)
            )
        elif result == CommitResult.SKIPPED_PRIVACY:
            self._event_logger.log(
                _make_event("memory_commit_skipped", [event.event_id], self._seq, reason="privacy_mode")
            )
        elif result == CommitResult.SKIPPED_CAMERA:
            self._event_logger.log(
                _make_event("memory_commit_skipped", [event.event_id], self._seq, reason="no_camera_memory_visual")
            )

    async def _handle_explicit_forget(self, event: Event) -> None:
        """Tombstone every active item matching payload['query'] across all stores.

        Bi-temporal tombstone (sets valid_to, leaves superseded_by None) per
        MemoryManager Protocol contract. Invariant #3: memory items retain
        provenance — items are NOT physically deleted.
        """
        payload = self._read_payload(event)
        query = payload.get("query", "")
        if not query:
            return

        tombstoned: list[tuple[str, str]] = []  # (store_name, item_id)
        for store_name, store in self._stores.items():
            try:
                matches = store.retrieve(query, top_k=10)
            except Exception as exc:
                sys.stderr.write(
                    f"[SleepTimeAgent] retrieve error on store {store_name}: {exc}\n"
                )
                continue
            for item in matches:
                try:
                    store.forget(item.item_id)
                except Exception as exc:
                    sys.stderr.write(
                        f"[SleepTimeAgent] forget error on store {store_name}: {exc}\n"
                    )
                    continue
                tombstoned.append((store_name, item.item_id))

        self._seq += 1
        completion = _make_event(
            "memory_tombstone_completed",
            [event.event_id],
            self._seq,
            reason=f"matched_count={len(tombstoned)}",
        )
        # Inline payload so consumers can see which items were tombstoned without
        # needing the per-store source_event_id closure here.
        object.__setattr__(
            completion,
            "payload_dict",
            {
                "query": query,
                "tombstoned": [
                    {"store": s, "item_id": i} for s, i in tombstoned
                ],
            },
        )
        self._event_logger.log(completion)

    def _read_payload(self, event: Event) -> dict:
        if self._payload_reader is not None:
            return self._payload_reader(event.event_id) or {}
        return getattr(event, "payload_dict", {})

    def _finalize_provenance(self, payload: dict) -> MemoryItem:
        now = self._clock()
        item_id = payload.get("item_id") or self._item_id_factory()
        content = payload["content"]

        if self._provenance_computer is not None and "confidence" not in payload and "salience" not in payload:
            confidence, salience, content_summary = self._provenance_computer.compute(payload)
            content_summary = content_summary[:SUMMARY_TRUNCATE_CHARS]
        else:
            confidence = payload.get("confidence", CONFIDENCE_DEFAULT)
            salience = payload.get("salience", SALIENCE_DEFAULT)
            content_summary = json.dumps(content)[:SUMMARY_TRUNCATE_CHARS]

        return MemoryItem(
            item_id=item_id,
            store=payload["store"],
            content=content,
            source_event_id=payload["source_event_id"],
            created_at=now,
            last_confirmed_at=now,
            confidence=confidence,
            salience=salience,
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
