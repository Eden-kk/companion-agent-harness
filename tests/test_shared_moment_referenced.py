"""v0.1g Task 16: shared moment retrieval contract test.

Success criterion (Anchor 3 / OQ-7):
- Populate episodic_memory with a "shared moment" item.
- Assert retrieve_shared_moments returns the item.
- Assert the projection is on-demand (reflects commits without re-instantiation).
- Assert ThinkerProposal can reference shared_moments via DecisionTrace.retrieval_used.
"""

from __future__ import annotations

import pathlib

import pytest

from companion_harness.episodic_memory_store import EpisodicMemoryStore
from companion_harness.schemas import (
    DecisionTrace,
    MemoryItem,
    PolicyInputs,
    ReasonCode,
    SensitiveField,
    ThinkerProposal,
)
from companion_harness.speak_policy import build_decision_trace, decide


def _make_moment(item_id: str, created_at: str = "2026-05-15T12:00:00+00:00") -> MemoryItem:
    return MemoryItem(
        item_id=item_id,
        store="episodic",
        content={"summary": f"shared walk in the park — {item_id}"},
        source_event_id="evt-orig-001",
        created_at=created_at,
        last_confirmed_at=created_at,
        confidence=0.95,
        salience=0.9,
        privacy_level="safe",
        mutability="system_revisable",
        valid_from=created_at,
        valid_to=None,
        superseded_by=None,
        user_visible_summary=SensitiveField(
            retention_policy_id="ep_default_30d",
            value=f"walk in the park — {item_id}",
        ),
    )


def test_retrieve_shared_moments_returns_committed_item(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_moment("moment-001"))
    results = store.retrieve_shared_moments(n=5)
    assert len(results) == 1
    assert results[0].item_id == "moment-001"


def test_retrieve_shared_moments_most_recent_first(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_moment("moment-001", "2026-05-13T00:00:00+00:00"))
    store.commit(_make_moment("moment-002", "2026-05-14T00:00:00+00:00"))
    store.commit(_make_moment("moment-003", "2026-05-15T00:00:00+00:00"))
    results = store.retrieve_shared_moments(n=5)
    ids = [r.item_id for r in results]
    assert ids == ["moment-003", "moment-002", "moment-001"]


def test_retrieve_shared_moments_projection_on_demand(tmp_path: pathlib.Path) -> None:
    """Anchor 3: projection is computed on-demand; reflects new commits immediately."""
    store = EpisodicMemoryStore(tmp_path / "ep")
    assert store.retrieve_shared_moments() == []

    store.commit(_make_moment("moment-001"))
    results = store.retrieve_shared_moments()
    assert len(results) == 1

    store.commit(_make_moment("moment-002", "2026-05-16T00:00:00+00:00"))
    results2 = store.retrieve_shared_moments()
    assert len(results2) == 2
    assert results2[0].item_id == "moment-002"


def test_retrieve_shared_moments_n_limit(tmp_path: pathlib.Path) -> None:
    store = EpisodicMemoryStore(tmp_path / "ep")
    for i in range(7):
        store.commit(_make_moment(f"moment-{i:03d}", f"2026-05-{10+i:02d}T00:00:00+00:00"))
    results = store.retrieve_shared_moments(n=5)
    assert len(results) == 5


def test_shared_moment_reference_in_decision_trace(tmp_path: pathlib.Path) -> None:
    """DecisionTrace.retrieval_used carries the shared-moment item_id (Anchor 3 attribution)."""
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_moment("moment-001"))

    moments = store.retrieve_shared_moments(n=1)
    assert moments[0].item_id == "moment-001"

    inputs = PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        aesthetic_novelty_score=0.8,
        retrieved_items=moments,
    )
    decision = decide(inputs, ["sig-1"])
    trace = build_decision_trace(
        decision,
        inputs,
        signal_event_ids=["sig-1"],
        decision_id="trace-001",
        retrieval_event_ids=[m.source_event_id for m in moments],
    )
    assert "evt-orig-001" in trace.retrieval_used


def test_proposal_with_shared_moment_context(tmp_path: pathlib.Path) -> None:
    """ThinkerProposal can reference a shared moment via caused_by (consumer projection)."""
    store = EpisodicMemoryStore(tmp_path / "ep")
    store.commit(_make_moment("moment-001"))
    moments = store.retrieve_shared_moments(n=1)

    proposal = ThinkerProposal(
        proposal_type="memory_bridge",
        content="we walked here before",
        trigger="scene_change",
        confidence=0.9,
        novelty=0.8,
        interruption_cost=0.1,
        max_utterance_ms=3000,
        cooldown_consumed="aesthetic_reaction",
        caused_by=[m.source_event_id for m in moments],
        rubric_violations=[],
    )
    assert proposal.caused_by == ["evt-orig-001"]
    assert len(moments) == 1
    assert moments[0].item_id == "moment-001"
