"""Task 13: explicit 'remember that' command — contract test (v0.1e).

Spec: docs/architecture-v0.1.md §Stage 4 lines 519-521.

Success criterion: user command "remember that I prefer X" →
  SleepTimeAgent emits memory_write_candidate event, then commits item
  to episodic_memory; capturing sink sees both event types; store has 1 item.
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from typing import Any

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event
from companion_harness.sleep_time_agent import SleepTimeAgent


async def _noop_sink(event: Event) -> None:
    pass


def _make_remember_event(event_id: str = "evt-remember-001") -> Event:
    """Simulate the orchestrator emitting memory_write_candidate after parsing
    user utterance 'remember that I prefer hiking'."""
    payload: dict[str, Any] = {
        "item_id": "item-prefer-hiking",
        "store": "episodic",
        "content": {"summary": "user prefers hiking"},
        "source_event_id": "evt-utterance-001",
        "privacy_mode": "normal",
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
        source="orchestrator",
        caused_by=["evt-utterance-001"],
        payload_hash="",
        payload_ref=None,
        payload_kind="memory_op",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="ep_default_30d",
    )
    object.__setattr__(evt, "payload_dict", payload)
    return evt


@pytest.mark.asyncio
async def test_remember_emits_candidate_then_commits(tmp_path: pathlib.Path) -> None:
    emitted: list[Event] = []

    async def capturing_sink(event: Event) -> None:
        emitted.append(event)

    store = EpisodicMemoryStore(tmp_path / "episodic")
    logger = EventLogger(capturing_sink)
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()

    candidate = _make_remember_event()
    await logger.start()
    logger.log(candidate)
    await logger.stop()

    # The candidate itself + the memory_commit_completed event
    types = [e.event_type for e in emitted]
    assert "memory_write_candidate" in types
    assert "memory_commit_completed" in types

    # Causal chain: completed event caused_by the candidate
    completed = next(e for e in emitted if e.event_type == "memory_commit_completed")
    assert "evt-remember-001" in completed.caused_by

    # Item committed to durable store
    results = store.retrieve("hiking")
    assert len(results) == 1
    assert results[0].item_id == "item-prefer-hiking"
    assert results[0].source_event_id == "evt-utterance-001"


@pytest.mark.asyncio
async def test_remember_provenance_fields_set(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "episodic")
    logger = EventLogger(_noop_sink)
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()

    await logger.start()
    logger.log(_make_remember_event())
    await logger.stop()

    item = store.retrieve("hiking")[0]
    assert item.created_at != ""
    assert item.valid_from != ""
    assert item.valid_to is None
    assert item.superseded_by is None
    assert item.user_visible_summary.value is not None
    assert item.confidence > 0
    assert item.salience > 0
