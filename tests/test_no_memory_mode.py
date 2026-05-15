"""Task 22: no_memory privacy mode — contract test (v0.1e).

Spec: docs/architecture-v0.1.md §Part 7 line 797 enum + Task 12 gate.

Success criterion: privacy_mode="no_memory" → ALL durable writes blocked;
  commit() silently skips (no exception); store remains empty.
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from typing import Any

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.sleep_time_agent import SleepTimeAgent


def _make_item(item_id: str = "item-nm-001") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": "user likes jazz"},
        source_event_id="evt-src-nm-001",
        created_at="2026-05-14T10:00:00+00:00",
        last_confirmed_at="2026-05-14T10:00:00+00:00",
        confidence=0.8,
        salience=0.5,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-14T10:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value="user likes jazz",
        ),
    )


def _make_candidate_event(event_id: str, privacy_mode: str) -> Event:
    payload: dict[str, Any] = {
        "item_id": "item-nm-001",
        "store": "episodic",
        "content": {"summary": "user likes jazz"},
        "source_event_id": "evt-src-nm-001",
        "privacy_mode": privacy_mode,
        "subject_class": "self",
        "privacy_level": "safe",
        "mutability": "system_revisable",
        "retention_policy_id": "ep_default_30d",
        "sensitivity": "safe",
    }
    evt = Event(
        event_id=event_id,
        session_id="test-session",
        schema_version="0.1",
        seq_no=1,
        event_type="memory_write_candidate",
        timestamp_mono_ms=int(time.monotonic() * 1000),
        timestamp_wall="",
        source="test",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="memory_op",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="ep_default_30d",
    )
    object.__setattr__(evt, "payload_dict", payload)
    return evt


async def _noop_sink(event: Event) -> None:
    pass


def test_no_memory_commit_blocked_directly(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    item = _make_item()
    # commit() must not raise — it silently skips
    store.commit(item, privacy_mode="no_memory")
    assert store.get("item-nm-001") is None


def test_no_memory_store_stays_empty(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    for i in range(5):
        item = _make_item(item_id=f"item-nm-{i:03d}")
        store.commit(item, privacy_mode="no_memory")
    results = store.retrieve("jazz", top_k=10)
    assert results == []


@pytest.mark.asyncio
async def test_no_memory_via_sleep_time_agent(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    logger = EventLogger(_noop_sink)
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()

    candidate = _make_candidate_event("evt-nm-agent-001", privacy_mode="no_memory")
    await logger.start()
    logger.log(candidate)
    await logger.stop()

    # no_memory goes through the store's commit() which silently skips
    assert store.get("item-nm-001") is None
