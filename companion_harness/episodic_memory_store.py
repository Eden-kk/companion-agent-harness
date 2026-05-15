"""EpisodicMemoryStore — durable, bi-temporally indexed MemoryManager (v0.1e Task 5).

Satisfies MemoryManager Protocol (companion_harness/memory_manager.py:27-32).
Per-item JSON files under <episodic_dir>/<item_id>.json.
Atomic-rename writes (mirrors decision_trace_store + core_user_profile_store).

ISO timestamp format:
  All timestamp strings (valid_from, valid_to, created_at, etc.) MUST use the
  "+00:00" suffix produced by datetime.now(timezone.utc).isoformat().
  Do NOT use "Z" suffix — "Z" is lexicographically less than "+" and breaks
  the string-comparison in _is_active(). v0.1f+ may normalize on serialize.

include_history semantics (retrieve):
  include_history=False (default): only _is_active() items returned.
  include_history=True: active + forgotten (valid_to set, superseded_by None)
    + superseded (superseded_by non-None). Hard-deleted items not returned
    (file is gone).

forget() diverges from core_user_profile_store.forget() by adding an
idempotency guard: if valid_to is already set (item is already forgotten OR
superseded), forget() returns without rewriting. This preserves the original
tombstone timestamp for the audit trail.

supersede() two-file write is NOT atomic across files. If the process crashes
between the old-item rewrite and the new-item write, the old item will have
superseded_by pointing at a non-existent file. Acceptable at v0.1e
(single-process). v0.1f+ may migrate to SQLite for transactional supersession.

Concurrency: single-process assumed; no file locking.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from datetime import datetime, timezone

from companion_harness.memory_manager import EmbeddingAdapter
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import MemoryItem, SensitiveField

__all__ = ["EpisodicMemoryStore"]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_active(item: MemoryItem) -> bool:
    if item.superseded_by is not None:
        return False
    if item.valid_to is None:
        return True
    return item.valid_to > _now_utc()


def _item_to_dict(item: MemoryItem) -> dict:
    return dataclasses.asdict(item)


def _item_from_dict(d: dict) -> MemoryItem:
    uvs = d.pop("user_visible_summary")
    return MemoryItem(**d, user_visible_summary=SensitiveField(**uvs))


class EpisodicMemoryStore:
    """Durable, append-only, bi-temporally indexed MemoryManager (v0.1e Task 5)."""

    def __init__(self, episodic_dir: pathlib.Path) -> None:
        self._dir = episodic_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, item_id: str) -> pathlib.Path:
        return self._dir / f"{item_id}.json"

    def _write(self, item: MemoryItem) -> None:
        path = self._path(item.item_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_item_to_dict(item), indent=2))
        os.rename(tmp, path)

    def _read(self, item_id: str) -> MemoryItem:
        d = json.loads(self._path(item_id).read_text())
        return _item_from_dict(d)

    # ------------------------------------------------------------------
    # MemoryManager Protocol
    # ------------------------------------------------------------------

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        try:
            check_privacy_gate(item, privacy_mode)
        except _SkipCommit:
            return
        self._write(item)

    def retrieve(
        self,
        query: str,
        top_k: int = 5,
        include_history: bool = False,
        embedder: EmbeddingAdapter | None = None,
    ) -> list[MemoryItem]:
        # embedder non-None signals caller wants embedding-based retrieval;
        # concrete implementation deferred — UNAVAILABLE: #183
        tokens = query.lower().split()
        results: list[MemoryItem] = []
        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json"):
                continue
            item_id = fname[:-5]
            item = self._read(item_id)
            if not include_history and not _is_active(item):
                continue
            text = json.dumps(item.content).lower()
            if any(t in text for t in tokens):
                results.append(item)
                if len(results) >= top_k:
                    break
        return results

    def forget(self, item_id: str) -> None:
        path = self._path(item_id)
        if not path.exists():
            return
        item = self._read(item_id)
        if item.valid_to is not None:
            return
        self._write(dataclasses.replace(item, valid_to=_now_utc()))

    def hard_delete(self, item_id: str) -> None:
        self._path(item_id).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # Episodic-specific (NOT part of the Protocol)
    # ------------------------------------------------------------------

    def supersede(self, old_item_id: str, new_item: MemoryItem) -> None:
        old = self._read(old_item_id)  # raises FileNotFoundError if missing
        if old.superseded_by is not None:
            raise ValueError(
                f"item {old_item_id} is already superseded; "
                f"supersede the leaf {old.superseded_by} instead"
            )
        now = _now_utc()
        self._write(dataclasses.replace(old, valid_to=now, superseded_by=new_item.item_id))
        self._write(new_item)

    def get(self, item_id: str) -> MemoryItem | None:
        path = self._path(item_id)
        if not path.exists():
            return None
        return self._read(item_id)
