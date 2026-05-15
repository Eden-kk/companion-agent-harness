"""MemoryManager — Protocol stub for the memory adapter seam (v0.1c Task 6).

See docs/architecture-v0.1.md §Part 3 (adapter architecture), §Part 6 Stage 4
(user-command surfaces), and §Part 7 (privacy_mode_compatibility).

## ADR note

The exact Protocol method set is provisional (spec does not pin it); the four
methods correspond to Part 6 Stage 4 user-command surfaces (`forget that`,
`what do you remember about me?`, plus write/retrieve as the underlying ops).
Refine at Stage-4 activation if needed.

The four spec stores (session, core_profile, episodic, semantic_relational)
appear only as the existing MemoryItem.store Literal in schemas.py — NOT
enumerated here.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from companion_harness.schemas import MemoryItem

__all__ = ["EmbeddingAdapter", "MemoryManager", "MemoryManagerStub", "_NullEmbeddingAdapter"]


@runtime_checkable
class EmbeddingAdapter(Protocol):
    def embed(self, text: str) -> list[float]:
        """Return embedding vector. Stub returns empty for lexical fallback."""
        ...


class _NullEmbeddingAdapter:
    # UNAVAILABLE: #183 — real embedding model pending selection (lexical baseline used)
    def embed(self, text: str) -> list[float]:
        return []


@runtime_checkable
class MemoryManager(Protocol):
    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None: ...
    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]: ...
    def forget(self, item_id: str) -> None: ...
    def hard_delete(self, item_id: str) -> None: ...


class MemoryManagerStub:
    """Inert skeleton — all methods raise NotImplementedError until Stage 4."""

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        raise NotImplementedError("MemoryManager.commit: wired at Stage 4")

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        raise NotImplementedError("MemoryManager.retrieve: wired at Stage 4")

    def forget(self, item_id: str) -> None:
        raise NotImplementedError("MemoryManager.forget: wired at Stage 4")

    def hard_delete(self, item_id: str) -> None:
        raise NotImplementedError("MemoryManager.hard_delete: wired at Stage 4")
