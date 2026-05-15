"""MemoryManager stub — unit tests (v0.1c Task 6).

Success criterion (verbatim):
  memory_manager.py imports cleanly; isinstance(MemoryManagerStub(), MemoryManager) is True;
  every method is inert (NotImplementedError); NO SDK import.
"""

import pytest

from companion_harness.memory_manager import MemoryManager, MemoryManagerStub
from companion_harness.schemas import MemoryItem


def _item() -> MemoryItem:
    return MemoryItem(
        item_id="item-1",
        store="session",
        content={},
        source_event_id="evt-1",
        created_at="2026-01-01T00:00:00Z",
        last_confirmed_at="2026-01-01T00:00:00Z",
        confidence=1.0,
        salience=1.0,
        privacy_level="default",
        mutability="system_revisable",
        valid_from="2026-01-01T00:00:00Z",
        valid_to=None,
        superseded_by=None,
        user_visible_summary="test",
    )


def test_isinstance_check():
    assert isinstance(MemoryManagerStub(), MemoryManager)


def test_write_candidate_raises():
    with pytest.raises(NotImplementedError):
        MemoryManagerStub().write_candidate(_item())


def test_retrieve_raises():
    with pytest.raises(NotImplementedError):
        MemoryManagerStub().retrieve("anything")


def test_forget_raises():
    with pytest.raises(NotImplementedError):
        MemoryManagerStub().forget("item-1")


def test_hard_delete_raises():
    with pytest.raises(NotImplementedError):
        MemoryManagerStub().hard_delete("item-1")


def test_no_sdk_import():
    import importlib
    import companion_harness.memory_manager as mm
    src = mm.__file__
    with open(src) as f:
        text = f.read()
    for sdk in ("torch", "onnxruntime", "sqlite3", "sqlalchemy", "chromadb", "faiss"):
        assert sdk not in text, f"SDK import found in memory_manager.py: {sdk}"
