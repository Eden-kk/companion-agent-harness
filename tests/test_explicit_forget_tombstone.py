"""Explicit `forget that` → bi-temporal tombstone (this PR).

Wires the gap between detection (`_detect_explicit_forget` in
realtime_orchestrator) and the per-store tombstone primitive (already on
all four MemoryManager implementations). SleepTimeAgent subscribes to
`explicit_forget`, fans out across wired stores via retrieve(query), and
calls forget(item_id) on each match (sets valid_to; leaves superseded_by
None). Closes the audit chain with `memory_tombstone_completed`.

Spec: docs/architecture-v0.1.md §Stage 4 lines 523-527, invariant #3.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.memory_manager import CommitResult
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.realtime_orchestrator import _detect_explicit_forget
from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.sleep_time_agent import SleepTimeAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _noop_sink(event: Event) -> None:
    pass


def _make_item(item_id: str, summary: str, source_event_id: str = "evt-src") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": summary},
        source_event_id=source_event_id,
        created_at="2026-05-14T10:00:00+00:00",
        last_confirmed_at="2026-05-14T10:00:00+00:00",
        confidence=0.9,
        salience=0.8,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from="2026-05-14T10:00:00+00:00",
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value=summary,
        ),
    )


def _make_explicit_forget_event(
    event_id: str = "evt-forget-001",
    query: str = "Acme",
    caused_by: list[str] | None = None,
) -> Event:
    evt = Event(
        event_id=event_id,
        session_id="test-session",
        schema_version="0.1",
        seq_no=1,
        event_type="explicit_forget",
        timestamp_mono_ms=int(time.monotonic() * 1000),
        timestamp_wall="",
        source="test",
        caused_by=caused_by or ["evt-transcript-1"],
        payload_hash="",
        payload_ref=None,
        payload_kind="memory_op",
        subject_class="self",
        sensitivity="sensitive",
        retention_policy_id="ep_default_30d",
    )
    object.__setattr__(evt, "payload_dict", {"query": query})
    return evt


class _InMemoryStore:
    """Minimal MemoryManager-compatible in-memory stub honoring bi-temporal forget."""

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> CommitResult:
        try:
            check_privacy_gate(item, privacy_mode)
        except _SkipCommit:
            return CommitResult.SKIPPED_PRIVACY
        except ValueError:
            return CommitResult.SKIPPED_CAMERA
        self._items[item.item_id] = item
        return CommitResult.COMMITTED

    def retrieve(self, query: str, top_k: int = 5, include_history: bool = False) -> list[MemoryItem]:
        q = query.lower()
        out: list[MemoryItem] = []
        for item in self._items.values():
            if not include_history and item.valid_to is not None:
                continue
            if q in str(item.content).lower():
                out.append(item)
            if len(out) >= top_k:
                break
        return out

    def forget(self, item_id: str) -> None:
        import dataclasses
        from datetime import datetime, timezone
        item = self._items.get(item_id)
        if item is None:
            return
        if item.valid_to is not None:
            return
        now = datetime.now(timezone.utc).isoformat()
        self._items[item_id] = dataclasses.replace(item, valid_to=now)

    def hard_delete(self, item_id: str) -> None:
        self._items.pop(item_id, None)

    def get(self, item_id: str) -> MemoryItem | None:
        return self._items.get(item_id)


async def _drive(logger: EventLogger, event: Event) -> None:
    await logger.start()
    logger.log(event)
    await logger.stop()


# ---------------------------------------------------------------------------
# Detector tests
# ---------------------------------------------------------------------------


def test_detector_matches_forget_that() -> None:
    matched, query = _detect_explicit_forget("forget that I work at Acme")
    assert matched
    assert query == "I work at Acme"


def test_detector_matches_forget_about() -> None:
    matched, query = _detect_explicit_forget("forget about my address")
    assert matched
    assert query == "my address"


def test_detector_matches_please_forget() -> None:
    matched, query = _detect_explicit_forget("please forget the meeting")
    assert matched
    assert query == "the meeting"


def test_detector_rejects_bare_forget_that() -> None:
    matched, query = _detect_explicit_forget("forget that")
    assert not matched
    assert query is None


def test_detector_rejects_unrelated_phrase() -> None:
    matched, query = _detect_explicit_forget("do not forget the milk")
    assert not matched
    assert query is None


# ---------------------------------------------------------------------------
# Wiring tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_explicit_forget_event_triggers_tombstone() -> None:
    """SleepTimeAgent on explicit_forget → store.forget() sets valid_to."""
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    store.commit(_make_item("item-1", "user works at Acme Corp"))
    assert store.get("item-1").valid_to is None

    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    await _drive(logger, _make_explicit_forget_event(query="Acme"))

    item = store.get("item-1")
    assert item is not None
    assert item.valid_to is not None
    assert item.superseded_by is None  # tombstone, not correction


@pytest.mark.asyncio
async def test_tombstoned_item_not_returned_by_default_retrieve() -> None:
    """After tombstone, default retrieve() must skip the item."""
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    store.commit(_make_item("item-1", "user works at Acme Corp"))

    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    await _drive(logger, _make_explicit_forget_event(query="Acme"))

    results = store.retrieve("Acme")
    assert not any(r.item_id == "item-1" for r in results)

    # And visible with include_history=True (audit invariant).
    history = store.retrieve("Acme", include_history=True)
    assert any(r.item_id == "item-1" for r in history)


@pytest.mark.asyncio
async def test_tombstone_only_matches_query_relevant_items() -> None:
    """Items not matching the query string must NOT be tombstoned."""
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    store.commit(_make_item("item-acme", "user works at Acme Corp"))
    store.commit(_make_item("item-other", "user likes hiking on weekends"))

    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    await _drive(logger, _make_explicit_forget_event(query="Acme"))

    assert store.get("item-acme").valid_to is not None
    assert store.get("item-other").valid_to is None


@pytest.mark.asyncio
async def test_tombstone_completion_event_caused_by_forget_event() -> None:
    """memory_tombstone_completed must close the caused_by chain to explicit_forget."""
    emitted: list[Event] = []

    async def _capture(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(_capture)
    store = _InMemoryStore()
    store.commit(_make_item("item-1", "user works at Acme Corp"))

    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    forget_evt = _make_explicit_forget_event(event_id="evt-forget-abc", query="Acme")
    await _drive(logger, forget_evt)

    completions = [e for e in emitted if e.event_type == "memory_tombstone_completed"]
    assert len(completions) == 1
    assert "evt-forget-abc" in completions[0].caused_by


@pytest.mark.asyncio
async def test_tombstone_fans_out_across_multiple_stores() -> None:
    """One explicit_forget event tombstones matching items in every wired store."""
    logger = EventLogger(_noop_sink)
    episodic = _InMemoryStore()
    semantic = _InMemoryStore()
    episodic.commit(_make_item("ep-1", "Acme employer record"))
    semantic.commit(_make_item("sem-1", "Acme is in Pittsburgh"))

    agent = SleepTimeAgent({"episodic": episodic, "semantic": semantic}, logger)
    await agent.start()
    await _drive(logger, _make_explicit_forget_event(query="Acme"))

    assert episodic.get("ep-1").valid_to is not None
    assert semantic.get("sem-1").valid_to is not None


@pytest.mark.asyncio
async def test_explicit_forget_no_matches_emits_zero_count_completion() -> None:
    """No matching items → completion event with matched_count=0 (no orphan)."""
    emitted: list[Event] = []

    async def _capture(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(_capture)
    store = _InMemoryStore()
    store.commit(_make_item("item-1", "user likes hiking"))

    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    await _drive(logger, _make_explicit_forget_event(query="Acme"))

    completions = [e for e in emitted if e.event_type == "memory_tombstone_completed"]
    assert len(completions) == 1
    # Item not touched.
    assert store.get("item-1").valid_to is None


@pytest.mark.asyncio
async def test_empty_query_payload_is_no_op() -> None:
    """Defensive: empty query string must not tombstone every item in the store."""
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    store.commit(_make_item("item-1", "user works at Acme Corp"))

    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    await _drive(logger, _make_explicit_forget_event(query=""))

    assert store.get("item-1").valid_to is None
