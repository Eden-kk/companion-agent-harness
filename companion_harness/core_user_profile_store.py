"""CoreUserProfileStore — durable, single-row-per-user JSON-on-disk store (v0.1e Task 8).

Per OQ-3 closed decision: JSON-on-disk with atomic-rename writes.
Items keyed by MemoryItem.item_id; retrieval is lexical (no embeddings until v0.1f+).
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from datetime import datetime, timezone

from companion_harness.memory_manager import CommitResult
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import MemoryItem, SensitiveField

__all__ = ["CoreUserProfileStore"]


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_active(item: MemoryItem) -> bool:
    if item.superseded_by is not None:
        return False
    if item.valid_to is None:
        return True
    return item.valid_to > _now_utc()


def _item_to_dict(item: MemoryItem) -> dict:
    d = dataclasses.asdict(item)
    # SensitiveField is a dataclass → asdict already recurses into it; no extra work needed.
    return d


def _item_from_dict(d: dict) -> MemoryItem:
    uvs = d.pop("user_visible_summary")
    item = MemoryItem(
        **d,
        user_visible_summary=SensitiveField(**uvs),
    )
    return item


class CoreUserProfileStore:
    """Satisfies MemoryManager Protocol; persists to a single JSON file."""

    def __init__(self, profile_path: pathlib.Path) -> None:
        self._path = profile_path

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, data: dict[str, dict]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.rename(tmp, self._path)

    # ------------------------------------------------------------------
    # MemoryManager Protocol
    # ------------------------------------------------------------------

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> CommitResult:
        try:
            check_privacy_gate(item, privacy_mode)
        except _SkipCommit:
            return CommitResult.SKIPPED_PRIVACY
        except ValueError:
            return CommitResult.SKIPPED_CAMERA
        data = self._load()
        data[item.item_id] = _item_to_dict(item)
        self._save(data)
        return CommitResult.COMMITTED

    def retrieve(self, query: str, top_k: int = 5, include_history: bool = False) -> list[MemoryItem]:
        data = self._load()
        tokens = query.lower().split()
        results: list[MemoryItem] = []
        for raw in data.values():
            item = _item_from_dict(dict(raw))
            if not include_history and not _is_active(item):
                continue
            text = json.dumps(item.content).lower()
            if any(t in text for t in tokens):
                results.append(item)
        return results[:top_k]

    def forget(self, item_id: str) -> None:
        data = self._load()
        if item_id in data:
            data[item_id]["valid_to"] = _now_utc()
            self._save(data)

    def hard_delete(self, item_id: str) -> None:
        data = self._load()
        data.pop(item_id, None)
        self._save(data)

    def retrieve_shared_moments(self, n: int = 5) -> list[MemoryItem]:
        return []
