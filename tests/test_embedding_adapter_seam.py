"""EmbeddingAdapter Protocol seam — contract tests (v0.1j Task 13).

Success criterion:
  EmbeddingAdapter is runtime_checkable;
  _NullEmbeddingAdapter returns [];
  EpisodicMemoryStore.retrieve() preserves lexical fallback when embedder=None;
  _NullEmbeddingAdapter carries the UNAVAILABLE: #183 marker.
"""

import inspect
import pathlib

import pytest

from companion_harness.memory_manager import EmbeddingAdapter, _NullEmbeddingAdapter
from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import MemoryItem, SensitiveField


def _make_item(item_id: str = "ep-001") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": "user visited Seattle"},
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
            value="user visited Seattle",
        ),
    )


def test_embedding_adapter_protocol_runtime_checkable():
    assert isinstance(_NullEmbeddingAdapter(), EmbeddingAdapter)


def test_null_embedding_adapter_returns_empty():
    adapter = _NullEmbeddingAdapter()
    assert adapter.embed("anything") == []


def test_memory_manager_retrieve_lexical_fallback_when_embedder_none(
    tmp_path: pathlib.Path,
) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    store.commit(_make_item())
    results = store.retrieve("Seattle", embedder=None)
    assert len(results) == 1
    assert results[0].item_id == "ep-001"


def test_null_embedding_adapter_marker_exists():
    import companion_harness.memory_manager as mm

    src = pathlib.Path(mm.__file__).read_text()
    assert "# UNAVAILABLE: #183" in src
