"""MiniCPMProvenanceComputer — unit + GPU integration tests (issue #188).

Tests:
  test_provenance_computer_returns_valid_tuple
      — fake model; asserts return-type and range contract.
  test_sleep_time_agent_accepts_provenance_computer_kwarg
      — SleepTimeAgent accepts and stores the kwarg without error.
  test_fallback_to_stub_when_computer_none
      — regression guard: None computer reproduces original stub-default behavior.

GPU test (requires b200 venv):
  test_provenance_computer_real_inference
      — real MiniCPMDuplexModel.chat() is called; tuple passes range checks.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.memory_manager import CommitResult
from companion_harness.privacy_gates import _SkipCommit, check_privacy_gate
from companion_harness.provenance_minicpm import (
    MiniCPMProvenanceComputer,
    _NullProvenanceComputer,
)
from companion_harness.schemas import Event, MemoryItem, SensitiveField
from companion_harness.sleep_time_agent import (
    CONFIDENCE_DEFAULT,
    SALIENCE_DEFAULT,
    SleepTimeAgent,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeProvenanceModel:
    """Returns a parseable three-line provenance response."""

    def chat(self, text: str, max_new_tokens: int = 64) -> str:
        return "confidence: 0.9\nsalience: 0.7\nsummary: user enjoys hiking in the mountains"


class _FakeGarbageModel:
    """Returns unparseable text — must fall back to defaults."""

    def chat(self, text: str, max_new_tokens: int = 64) -> str:
        return "I have no idea"


class _FakeRaisingModel:
    """Raises on chat() — must fall back to defaults."""

    def chat(self, text: str, max_new_tokens: int = 64) -> str:
        raise RuntimeError("model unavailable")


class _InMemoryStore:
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


async def _noop_sink(event: Event) -> None:
    pass


def _make_candidate_event(event_id: str = "evt-cand-001", **payload_overrides: object) -> Event:
    payload = {
        "item_id": "item-abc123",
        "store": "episodic",
        "content": {"summary": "user likes hiking"},
        "source_event_id": "evt-src-001",
        "privacy_mode": "normal",
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


async def _run_agent_with_event(agent: SleepTimeAgent, logger: EventLogger, event: Event) -> None:
    await logger.start()
    logger.log(event)
    await logger.stop()


# ---------------------------------------------------------------------------
# CPU tests
# ---------------------------------------------------------------------------


def test_provenance_computer_returns_valid_tuple() -> None:
    computer = MiniCPMProvenanceComputer(_FakeProvenanceModel())  # type: ignore[arg-type]
    confidence, salience, summary = computer.compute({"content": {"text": "user likes hiking"}})
    assert isinstance(confidence, float)
    assert isinstance(salience, float)
    assert isinstance(summary, str)
    assert 0.0 <= confidence <= 1.0
    assert 0.0 <= salience <= 1.0
    assert len(summary) <= 80


def test_provenance_computer_parses_values() -> None:
    computer = MiniCPMProvenanceComputer(_FakeProvenanceModel())  # type: ignore[arg-type]
    confidence, salience, summary = computer.compute({"content": {"text": "user likes hiking"}})
    assert confidence == pytest.approx(0.9)
    assert salience == pytest.approx(0.7)
    assert "hiking" in summary


def test_provenance_computer_garbage_falls_back_to_defaults() -> None:
    computer = MiniCPMProvenanceComputer(_FakeGarbageModel())  # type: ignore[arg-type]
    confidence, salience, _ = computer.compute({"content": {"text": "something"}})
    assert confidence == CONFIDENCE_DEFAULT
    assert salience == SALIENCE_DEFAULT


def test_provenance_computer_raising_model_falls_back_to_defaults() -> None:
    computer = MiniCPMProvenanceComputer(_FakeRaisingModel())  # type: ignore[arg-type]
    confidence, salience, _ = computer.compute({"content": {}})
    assert confidence == CONFIDENCE_DEFAULT
    assert salience == SALIENCE_DEFAULT


def test_null_provenance_computer_returns_defaults() -> None:
    computer = _NullProvenanceComputer()
    confidence, salience, summary = computer.compute({"content": {"x": 1}})
    assert confidence == CONFIDENCE_DEFAULT
    assert salience == SALIENCE_DEFAULT
    assert isinstance(summary, str)


def test_sleep_time_agent_accepts_provenance_computer_kwarg() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    computer = MiniCPMProvenanceComputer(_FakeProvenanceModel())  # type: ignore[arg-type]
    agent = SleepTimeAgent({"episodic": store}, logger, provenance_computer=computer)
    assert agent._provenance_computer is computer


@pytest.mark.asyncio
async def test_provenance_computer_drives_confidence_salience() -> None:
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    computer = MiniCPMProvenanceComputer(_FakeProvenanceModel())  # type: ignore[arg-type]
    agent = SleepTimeAgent({"episodic": store}, logger, provenance_computer=computer)
    await agent.start()
    evt = _make_candidate_event()
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert item.confidence == pytest.approx(0.9)
    assert item.salience == pytest.approx(0.7)
    assert item.user_visible_summary.value is not None
    assert len(item.user_visible_summary.value) <= 80


@pytest.mark.asyncio
async def test_fallback_to_stub_when_computer_none() -> None:
    """Regression guard: None computer reproduces original stub-default behavior."""
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    agent = SleepTimeAgent({"episodic": store}, logger, provenance_computer=None)
    await agent.start()
    evt = _make_candidate_event()
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert item.confidence == CONFIDENCE_DEFAULT
    assert item.salience == SALIENCE_DEFAULT


@pytest.mark.asyncio
async def test_payload_hint_takes_precedence_over_computer() -> None:
    """Explicit confidence/salience in payload bypasses the LLM computer."""
    logger = EventLogger(_noop_sink)
    store = _InMemoryStore()
    computer = MiniCPMProvenanceComputer(_FakeProvenanceModel())  # type: ignore[arg-type]
    agent = SleepTimeAgent({"episodic": store}, logger, provenance_computer=computer)
    await agent.start()
    evt = _make_candidate_event(confidence=0.42, salience=0.11)
    await _run_agent_with_event(agent, logger, evt)
    item = store.committed[0]
    assert item.confidence == pytest.approx(0.42)
    assert item.salience == pytest.approx(0.11)


# ---------------------------------------------------------------------------
# GPU test (b200 venv required)
# ---------------------------------------------------------------------------


torch = pytest.importorskip("torch", reason="torch not available — b200 venv required")

if not torch.cuda.is_available():
    pytest.skip("CUDA not available — b200 required", allow_module_level=True)


@pytest.mark.gpu
def test_provenance_computer_real_inference() -> None:
    """Real MiniCPMDuplexModel.chat() is called; tuple passes range checks."""
    from companion_harness.foreground_model_minicpm import MiniCPMDuplexModel

    model = MiniCPMDuplexModel()
    computer = MiniCPMProvenanceComputer(model)
    candidate = {"content": {"summary": "user mentioned they enjoy hiking every weekend"}}
    confidence, salience, summary = computer.compute(candidate)
    assert 0.0 <= confidence <= 1.0, f"confidence out of range: {confidence}"
    assert 0.0 <= salience <= 1.0, f"salience out of range: {salience}"
    assert isinstance(summary, str)
    assert len(summary) <= 80
