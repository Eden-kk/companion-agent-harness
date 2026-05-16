"""Invariant-#1 completeness: every skip path emits memory_commit_skipped (issue #113).

All 3 skip paths (no_memory/sensitive_conversation, no_camera_memory+visual, guest_present)
must emit memory_commit_skipped — not silently drop the event.
"""

from __future__ import annotations

import pathlib
import time
from typing import Any

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.event_logger import EventLogger
from companion_harness.memory_manager import CommitResult
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.semantic_relational_store import SemanticRelationalStore
from companion_harness.session_state_store import SessionStateStore
from companion_harness.sleep_time_agent import SleepTimeAgent
from companion_harness.core_user_profile_store import CoreUserProfileStore


async def _noop_sink(event: Event) -> None:
    pass


def _make_candidate_event(
    event_id: str = "evt-cand-001",
    store: str = "episodic",
    privacy_mode: str = "normal",
    content: dict | None = None,
    retention_policy_id: str = "ep_default_30d",
    **payload_overrides: Any,
) -> Event:
    payload: dict[str, Any] = {
        "item_id": "item-abc123",
        "store": store,
        "content": content if content is not None else {"summary": "user likes hiking"},
        "source_event_id": "evt-src-001",
        "privacy_mode": privacy_mode,
        "subject_class": "self",
        "privacy_level": "safe",
        "mutability": "system_revisable",
        "retention_policy_id": retention_policy_id,
        "sensitivity": "safe",
    }
    payload.update(payload_overrides)

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


async def _run(agent: SleepTimeAgent, logger: EventLogger, event: Event) -> None:
    await logger.start()
    logger.log(event)
    await logger.stop()


def _make_real_store_agent(store_key: str, store: Any, logger: EventLogger) -> SleepTimeAgent:
    return SleepTimeAgent({store_key: store}, logger)


# ---------------------------------------------------------------------------
# no_memory → SKIPPED_PRIVACY → memory_commit_skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_skipped_event_emitted_for_episodic_no_memory(tmp_path: pathlib.Path) -> None:
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = EpisodicMemoryStore(tmp_path / "ep")
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(event_id="evt-nm-ep", privacy_mode="no_memory")
    await _run(agent, logger, candidate)

    skipped = [e for e in emitted if e.event_type == "memory_commit_skipped"]
    assert len(skipped) == 1
    assert "evt-nm-ep" in skipped[0].caused_by
    assert skipped[0].payload_ref == "privacy_mode"


@pytest.mark.asyncio
async def test_commit_skipped_event_emitted_for_session_state_sensitive_conversation() -> None:
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = SessionStateStore()
    agent = SleepTimeAgent({"session": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(
        event_id="evt-sc-ss",
        store="session",
        privacy_mode="sensitive_conversation",
        retention_policy_id="ep_default_30d",
    )
    await _run(agent, logger, candidate)

    skipped = [e for e in emitted if e.event_type == "memory_commit_skipped"]
    assert len(skipped) == 1
    assert "evt-sc-ss" in skipped[0].caused_by
    assert skipped[0].payload_ref == "privacy_mode"


@pytest.mark.asyncio
async def test_commit_skipped_event_emitted_for_core_user_profile_no_memory(tmp_path: pathlib.Path) -> None:
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = CoreUserProfileStore(tmp_path / "profile.json")
    agent = SleepTimeAgent({"core_profile": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(
        event_id="evt-nm-cup", store="core_profile", privacy_mode="no_memory"
    )
    await _run(agent, logger, candidate)

    skipped = [e for e in emitted if e.event_type == "memory_commit_skipped"]
    assert len(skipped) == 1
    assert "evt-nm-cup" in skipped[0].caused_by
    assert skipped[0].payload_ref == "privacy_mode"


@pytest.mark.asyncio
async def test_commit_skipped_event_emitted_for_semantic_relational_no_memory(tmp_path: pathlib.Path) -> None:
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = SemanticRelationalStore(tmp_path / "sr")
    agent = SleepTimeAgent({"semantic_relational": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(
        event_id="evt-nm-sr",
        store="semantic_relational",
        privacy_mode="no_memory",
        content={"subject": "alice", "predicate": "likes", "object": "tea"},
    )
    await _run(agent, logger, candidate)

    skipped = [e for e in emitted if e.event_type == "memory_commit_skipped"]
    assert len(skipped) == 1
    assert "evt-nm-sr" in skipped[0].caused_by
    assert skipped[0].payload_ref == "privacy_mode"


# ---------------------------------------------------------------------------
# no_camera_memory + visual → SKIPPED_CAMERA → memory_commit_skipped
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_skipped_event_emitted_for_episodic_no_camera_memory_visual(tmp_path: pathlib.Path) -> None:
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = EpisodicMemoryStore(tmp_path / "ep")
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(
        event_id="evt-cam-ep",
        privacy_mode="no_camera_memory",
        content={"frame_id": "f-001"},
    )
    await _run(agent, logger, candidate)

    skipped = [e for e in emitted if e.event_type == "memory_commit_skipped"]
    assert len(skipped) == 1
    assert "evt-cam-ep" in skipped[0].caused_by
    assert skipped[0].payload_ref == "no_camera_memory_visual"


# ---------------------------------------------------------------------------
# guest_present → SKIPPED_PRIVACY → memory_commit_skipped (existing path, regression guard)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_skipped_event_emitted_for_guest_present(tmp_path: pathlib.Path) -> None:
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = EpisodicMemoryStore(tmp_path / "ep")
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(event_id="evt-gp-ep", privacy_mode="guest_present")
    await _run(agent, logger, candidate)

    skipped = [e for e in emitted if e.event_type == "memory_commit_skipped"]
    assert len(skipped) == 1
    assert "evt-gp-ep" in skipped[0].caused_by
    assert skipped[0].payload_ref == "privacy_mode"


# ---------------------------------------------------------------------------
# caused_by closes to memory_write_candidate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caused_by_closes_to_memory_write_candidate(tmp_path: pathlib.Path) -> None:
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = EpisodicMemoryStore(tmp_path / "ep")
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(event_id="evt-chain-001", privacy_mode="no_memory")
    await _run(agent, logger, candidate)

    outcome_events = [
        e for e in emitted
        if e.event_type in ("memory_commit_completed", "memory_commit_skipped")
    ]
    assert len(outcome_events) == 1
    assert "evt-chain-001" in outcome_events[0].caused_by


# ---------------------------------------------------------------------------
# Invariant #1 regression guard: no outcome event → causal gap → fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_causal_graph_completeness_under_commit_skip(tmp_path: pathlib.Path) -> None:
    """Every memory_write_candidate must produce exactly one outcome event."""
    emitted: list[Event] = []

    async def sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(sink)
    store = EpisodicMemoryStore(tmp_path / "ep")
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()

    candidates = [
        _make_candidate_event(event_id="evt-cg-1", privacy_mode="normal"),
        _make_candidate_event(event_id="evt-cg-2", privacy_mode="no_memory"),
        _make_candidate_event(event_id="evt-cg-3", privacy_mode="guest_present"),
        _make_candidate_event(
            event_id="evt-cg-4",
            privacy_mode="no_camera_memory",
            content={"frame_id": "f-x"},
        ),
    ]

    await logger.start()
    for c in candidates:
        logger.log(c)
    await logger.stop()

    candidate_ids = {c.event_id for c in candidates}
    outcome_events = [
        e for e in emitted
        if e.event_type in ("memory_commit_completed", "memory_commit_skipped")
    ]
    covered = {e.caused_by[0] for e in outcome_events if e.caused_by}
    assert candidate_ids == covered, (
        f"causal gap: candidates without outcome event = {candidate_ids - covered}"
    )
