"""SleepTimeAgent — unit tests (v0.1e Task 10).

Success criterion: all 20 tests pass.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.memory_manager import CommitResult
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.sleep_time_agent import (
    CONFIDENCE_DEFAULT,
    SALIENCE_DEFAULT,
    SUMMARY_TRUNCATE_CHARS,
    SleepTimeAgent,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _noop_sink(event: Event) -> None:
    pass


def _make_candidate_event(
    event_id: str = "evt-cand-001",
    store: str = "episodic",
    privacy_mode: str = "normal",
    **payload_overrides: Any,
) -> Event:
    payload: dict[str, Any] = {
        "item_id": "item-abc123",
        "store": store,
        "content": {"summary": "user likes hiking"},
        "source_event_id": "evt-src-001",
        "privacy_mode": privacy_mode,
        "subject_class": "self",
        "privacy_level": "safe",
        "mutability": "system_revisable",
        "retention_policy_id": "ep_default_30d",
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


def _make_non_candidate_event() -> Event:
    return Event(
        event_id="evt-vad-001",
        session_id="test-session",
        schema_version="0.1",
        seq_no=2,
        event_type="vad_frame",
        timestamp_mono_ms=int(time.monotonic() * 1000),
        timestamp_wall="",
        source="test",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="default",
    )


class _InMemoryStore:
    """Minimal MemoryManager-compatible in-memory stub for testing."""

    def __init__(self) -> None:
        self.committed: list[MemoryItem] = []

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> CommitResult:
        try:
            check_privacy_gate(item, privacy_mode)
        except _SkipCommit:
            return CommitResult.SKIPPED_PRIVACY
        except ValueError:
            return CommitResult.SKIPPED_CAMERA
        self.committed.append(item)
        return CommitResult.COMMITTED

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        return []

    def forget(self, item_id: str) -> None:
        pass

    def hard_delete(self, item_id: str) -> None:
        pass


class _RaisingStore:
    """Store whose commit always raises."""

    def commit(self, item: MemoryItem, privacy_mode: str = "normal") -> None:
        raise RuntimeError("disk full")

    def retrieve(self, query: str, top_k: int = 5) -> list[MemoryItem]:
        return []

    def forget(self, item_id: str) -> None:
        pass

    def hard_delete(self, item_id: str) -> None:
        pass


async def _run_agent_with_event(
    agent: SleepTimeAgent,
    logger: EventLogger,
    event: Event,
) -> None:
    await logger.start()
    logger.log(event)
    await logger.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_subscribe_then_start_attaches_to_logger() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    assert agent._on_event in logger._subscribers
    logger._subscribers.clear()


@pytest.mark.asyncio
async def test_memory_write_candidate_episodic_commits() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_candidate_event(store="episodic")
    await _run_agent_with_event(agent, logger, evt)
    assert len(store.committed) == 1
    assert store.committed[0].store == "episodic"


@pytest.mark.asyncio
async def test_memory_write_candidate_session_commits() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"session": store}, logger)
    await agent.start()
    evt = _make_candidate_event(store="session")
    await _run_agent_with_event(agent, logger, evt)
    assert len(store.committed) == 1
    assert store.committed[0].store == "session"


@pytest.mark.asyncio
async def test_memory_write_candidate_core_profile_commits() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"core_profile": store}, logger)
    await agent.start()
    evt = _make_candidate_event(store="core_profile")
    await _run_agent_with_event(agent, logger, evt)
    assert len(store.committed) == 1
    assert store.committed[0].store == "core_profile"


@pytest.mark.asyncio
async def test_provenance_fields_populated() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_candidate_event()
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert item.confidence == CONFIDENCE_DEFAULT
    assert item.salience == SALIENCE_DEFAULT
    assert item.created_at != ""
    assert item.valid_from != ""
    assert item.user_visible_summary.value is not None


@pytest.mark.asyncio
async def test_provenance_hint_passthrough() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_candidate_event(confidence=0.95, salience=0.3)
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert item.confidence == 0.95
    assert item.salience == 0.3


@pytest.mark.asyncio
async def test_user_visible_summary_truncates_to_80_chars() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    long_content = {"text": "x" * 200}
    evt = _make_candidate_event(content=long_content)
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert len(item.user_visible_summary.value) <= SUMMARY_TRUNCATE_CHARS


@pytest.mark.asyncio
async def test_user_visible_summary_carries_retention_policy_id() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_candidate_event(retention_policy_id="ep_default_30d")
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert item.user_visible_summary.retention_policy_id == "ep_default_30d"


@pytest.mark.asyncio
async def test_non_candidate_event_ignored() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_non_candidate_event()
    await _run_agent_with_event(agent, logger, evt)
    assert len(store.committed) == 0


@pytest.mark.asyncio
async def test_guest_present_pause() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_candidate_event(privacy_mode="guest_present")
    await _run_agent_with_event(agent, logger, evt)
    assert len(store.committed) == 0


@pytest.mark.asyncio
async def test_non_guest_present_modes_commit() -> None:
    # "normal" commits
    logger = EventLogger(_noop_sink)
    store_normal = _InMemoryStore()
    agent1 = SleepTimeAgent({"episodic": store_normal}, logger)
    await agent1.start()
    await logger.start()
    logger.log(_make_candidate_event(event_id="evt-1", privacy_mode="normal"))
    await logger.stop()
    assert len(store_normal.committed) == 1

    # missing privacy_mode key commits (not "guest_present")
    logger2 = EventLogger(_noop_sink)
    store_missing = _InMemoryStore()
    agent2 = SleepTimeAgent({"episodic": store_missing}, logger2)
    await agent2.start()
    await logger2.start()
    evt = _make_candidate_event(event_id="evt-2")
    payload_without_mode = {k: v for k, v in evt.payload_dict.items() if k != "privacy_mode"}
    object.__setattr__(evt, "payload_dict", payload_without_mode)
    logger2.log(evt)
    await logger2.stop()
    assert len(store_missing.committed) == 1


@pytest.mark.asyncio
async def test_unknown_store_drops_silently(capsys) -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_candidate_event(store="semantic_relational")
    await _run_agent_with_event(agent, logger, evt)
    assert len(store.committed) == 0
    captured = capsys.readouterr()
    assert "unknown/unwired store" in captured.err


@pytest.mark.asyncio
async def test_store_commit_exception_does_not_kill_drain() -> None:
    logger = EventLogger(_noop_sink)
    agent = SleepTimeAgent({"episodic": _RaisingStore()}, logger)
    await agent.start()
    await logger.start()
    logger.log(_make_candidate_event(event_id="evt-raise-1"))
    logger.log(_make_candidate_event(event_id="evt-raise-2"))
    await logger.stop()
    # drain survived both events without crashing


@pytest.mark.asyncio
async def test_smoke_no_latency_regression() -> None:
    """Fanout overhead vs no-agent control: p50 treatment <= p50 control * 1.05."""
    n = 1000

    async def _measure(use_agent: bool) -> float:
        logger = EventLogger(_noop_sink, maxsize=n + 10)
        if use_agent:
            store = _InMemoryStore()
            agent = SleepTimeAgent({"episodic": store}, logger)
            await agent.start()
        await logger.start()
        timings: list[float] = []
        for _ in range(n):
            t0 = time.monotonic()
            logger.log(_make_non_candidate_event())
            timings.append(time.monotonic() - t0)
        await logger.stop()
        timings.sort()
        return timings[n // 2]

    p50_control = await _measure(use_agent=False)
    p50_treatment = await _measure(use_agent=True)
    limit = max(p50_control * 1.05, 1e-6)
    assert p50_treatment <= limit, (
        f"latency regression: p50 control={p50_control:.6f}s "
        f"treatment={p50_treatment:.6f}s"
    )


@pytest.mark.asyncio
async def test_caused_by_chain_preserved() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    evt = _make_candidate_event(source_event_id="evt-src-xyz")
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert item.source_event_id == "evt-src-xyz"


@pytest.mark.asyncio
async def test_no_companion_state_mutation() -> None:
    """SC-1: agent must not import or touch companion_state."""
    import companion_harness.sleep_time_agent as mod
    source = mod.__file__
    with open(source) as f:
        text = f.read()
    assert "companion_state" not in text, "SC-1 violation: companion_state referenced in agent"


@pytest.mark.asyncio
async def test_stop_then_start_does_not_double_subscribe() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    await agent.stop()
    await agent.start()
    count = logger._subscribers.count(agent._on_event)
    assert count == 1

    await logger.start()
    logger.log(_make_candidate_event())
    await logger.stop()
    assert len(store.committed) == 1


@pytest.mark.asyncio
async def test_memory_commit_completed_emitted_on_success() -> None:
    emitted: list[Event] = []

    async def capturing_sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(capturing_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(event_id="evt-cand-emit")
    await _run_agent_with_event(agent, logger, candidate)

    completed = [e for e in emitted if e.event_type == "memory_commit_completed"]
    assert len(completed) == 1
    assert "evt-cand-emit" in completed[0].caused_by


@pytest.mark.asyncio
async def test_memory_commit_skipped_emitted_on_guest_present() -> None:
    emitted: list[Event] = []

    async def capturing_sink(event: Event) -> None:
        emitted.append(event)

    logger = EventLogger(capturing_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await agent.start()
    candidate = _make_candidate_event(
        event_id="evt-cand-guest", privacy_mode="guest_present"
    )
    await _run_agent_with_event(agent, logger, candidate)

    skipped = [e for e in emitted if e.event_type == "memory_commit_skipped"]
    assert len(skipped) == 1
    assert "evt-cand-guest" in skipped[0].caused_by
    assert len(store.committed) == 0


@pytest.mark.asyncio
async def test_agent_start_after_logger_start_succeeds() -> None:
    """SleepTimeAgent.start() after EventLogger.start() succeeds via late_subscribe().

    Previously SleepTimeAgent called EventLogger.subscribe(), which raises if the
    logger has already started. The agent now uses late_subscribe() so a shared
    logger (e.g. SharedLoggerProxy in the manual-test server) can be started
    once at Application boot and per-session SleepTimeAgents can subscribe later.
    """
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger)
    await logger.start()
    await agent.start()  # must NOT raise
    await agent.stop()
    await logger.stop()
