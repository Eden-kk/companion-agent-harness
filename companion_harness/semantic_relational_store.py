"""SemanticRelationalStore — durable, bi-temporally indexed MemoryManager (v0.1e Task 9).

Satisfies MemoryManager Protocol (companion_harness/memory_manager.py:27-32).
Per-item JSON files under <semantic_dir>/<item_id>.json.
Atomic-rename writes (mirrors episodic_memory_store.py).

Anchor 1 content shape (LOCKED):
  Every item.content MUST contain:
    "subject":   non-empty str
    "predicate": non-empty str
    "object":    non-empty str
    "qualifiers": optional dict; if present, all values must be JSON-serializable.
  Enforced at commit() and supersede() entry points via _validate_content_shape().

ISO timestamp format:
  All timestamp strings use "+00:00" suffix from datetime.now(timezone.utc).isoformat().
  Do NOT use "Z" suffix.

Retrieval uses a structured token bag built from [subject, predicate, object] plus
qualifier VALUES (keys excluded to avoid field-name pollution). A query of "subject"
or "qualifiers" will NOT match any item — only field values contribute to the bag.

forget() idempotency guard: if valid_to is already set (item is already forgotten or
superseded), forget() returns without rewriting, preserving the original tombstone
timestamp. This diverges from core_user_profile_store.forget() which always rewrites.

supersede() two-file write is NOT atomic across files. Acceptable at v0.1e
(single-process). v0.1f+ may migrate to SQLite for transactional supersession.

Concurrency: single-process assumed; no file locking.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
from datetime import datetime, timezone

from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import MemoryItem, SensitiveField

__all__ = ["SemanticRelationalStore"]


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


def _validate_content_shape(item: MemoryItem) -> None:
    """Enforce Anchor 1 S-P-O+qualifiers contract; raise ValueError on violation."""
    content = item.content
    item_id = item.item_id

    if not isinstance(content, dict):
        raise ValueError(f"{item_id}: content must be a dict, got {type(content).__name__}")

    for field in ("subject", "predicate", "object"):
        if field not in content:
            raise ValueError(f"{item_id}: content missing required field '{field}'")
        val = content[field]
        if not isinstance(val, str) or not val:
            raise ValueError(
                f"{item_id}: content['{field}'] must be a non-empty str, got {val!r}"
            )

    if "qualifiers" in content:
        qualifiers = content["qualifiers"]
        if not isinstance(qualifiers, dict):
            raise ValueError(
                f"{item_id}: content['qualifiers'] must be a dict, got {type(qualifiers).__name__}"
            )
        for k, v in qualifiers.items():
            try:
                json.dumps(v)
            except TypeError:
                raise ValueError(
                    f"{item_id}: content['qualifiers'][{k!r}] is not JSON-serializable"
                )


class SemanticRelationalStore:
    """Durable, append-only, bi-temporally indexed MemoryManager (v0.1e Task 9).

    Satisfies MemoryManager Protocol (companion_harness/memory_manager.py:27-32).
    Per-item JSON files under <semantic_dir>/<item_id>.json.
    Content shape locked by Anchor 1: every item.content MUST contain
    "subject", "predicate", "object" (non-empty str); "qualifiers" is optional dict.
    """

    def __init__(self, semantic_dir: pathlib.Path) -> None:
        self._dir = semantic_dir
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
        _validate_content_shape(item)
        self._write(item)

    def retrieve(
        self, query: str, top_k: int = 5, include_history: bool = False
    ) -> list[MemoryItem]:
        tokens = query.lower().split()
        results: list[MemoryItem] = []
        for fname in sorted(os.listdir(self._dir)):
            if not fname.endswith(".json"):
                continue
            item_id = fname[:-5]
            item = self._read(item_id)
            if not include_history and not _is_active(item):
                continue
            content = item.content
            bag_parts = [
                content.get("subject", ""),
                content.get("predicate", ""),
                content.get("object", ""),
            ]
            for v in (content.get("qualifiers") or {}).values():
                bag_parts.append(json.dumps(v, ensure_ascii=False).lower())
            bag = " ".join(p.lower() if isinstance(p, str) else p for p in bag_parts)
            if any(qt in bag for qt in tokens):
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

    def retrieve_shared_moments(self, n: int = 5) -> list[MemoryItem]:
        return []

    # ------------------------------------------------------------------
    # Semantic-relational-specific (NOT part of the Protocol)
    # ------------------------------------------------------------------

    def supersede(self, old_item_id: str, new_item: MemoryItem) -> None:
        _validate_content_shape(new_item)
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
