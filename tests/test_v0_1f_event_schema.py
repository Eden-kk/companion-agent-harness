"""Tests for the v0.1f event-type payload schema registry (Anchor 2).

Success criterion (verbatim from docs/roadmap-v0.1f-draft.md Task 1):
  schema file lands; registry YAML has 2 new entries; alphabet locked;
  ToolProgressEvidence dataclass present; PolicyInputs.tool_progress_evidence
  field present with default None; no v0.1a–e test regressions.

These tests validate ONLY the schema contract.  Downstream behavior
(ToolRouter, ToolProgressEmitter, BackgroundReasoner) is out of scope for
Task 1 and lives in Tasks 3, 4, 8.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from companion_harness.v0_1f_event_schema import (
    EVENT_TYPE_SCHEMAS,
    ToolEventSchema,
    DERIVED_FROM_PROGRESS,
    DERIVED_FROM_REQUEST,
    DERIVED_FROM_RESULT,
)


# --- The 5 new tool_* event types (Anchor 2) ----------------------------------

EXPECTED_EVENT_TYPES = {
    "tool_call_requested",
    "tool_call_dispatched",
    "tool_progress_event",
    "tool_call_completed",
    "tool_call_cancelled",
}


def test_registry_has_exactly_five_event_types():
    assert set(EVENT_TYPE_SCHEMAS.keys()) == EXPECTED_EVENT_TYPES


@pytest.mark.parametrize("event_type", sorted(EXPECTED_EVENT_TYPES))
def test_every_event_type_payload_kind_is_tool_event(event_type):
    """Anchor 2 row: payload_kind = `tool_event` for all 5 tool_* event types."""
    assert EVENT_TYPE_SCHEMAS[event_type].payload_kind == "tool_event"


@pytest.mark.parametrize("event_type", sorted(EXPECTED_EVENT_TYPES))
def test_every_event_type_subject_class_is_self(event_type):
    """Anchor 2 row: subject_class = `self` for all 5 tool_* event types."""
    assert EVENT_TYPE_SCHEMAS[event_type].subject_class == "self"


@pytest.mark.parametrize("event_type", sorted(EXPECTED_EVENT_TYPES))
def test_every_event_type_carries_tool_call_id(event_type):
    """Anchor 2 patched cross-bullet: all 5 tool_* events carry tool_call_id."""
    assert "tool_call_id" in EVENT_TYPE_SCHEMAS[event_type].required_fields


def test_tool_call_dispatched_carries_routing_tier():
    """Anchor 3: routing_tier is recorded on tool_call_dispatched only."""
    assert "routing_tier" in EVENT_TYPE_SCHEMAS["tool_call_dispatched"].required_fields


def test_tool_progress_event_carries_progress_stage():
    """Anchor 1: progress_stage is recorded on tool_progress_event."""
    assert "progress_stage" in EVENT_TYPE_SCHEMAS["tool_progress_event"].required_fields


def test_fully_static_event_types_have_concrete_classification():
    """Anchor 2: dispatched + cancelled have safe/tool_call_audit_30d static."""
    for event_type in ("tool_call_dispatched", "tool_call_cancelled"):
        schema = EVENT_TYPE_SCHEMAS[event_type]
        assert schema.sensitivity == "safe"
        assert schema.retention_policy_id == "tool_call_audit_30d"


def test_derived_event_types_use_sentinels():
    """Anchor 2: requested / progress / completed derive sensitivity + retention
    from their payload at emission time (sentinels are None)."""
    assert EVENT_TYPE_SCHEMAS["tool_call_requested"].sensitivity is DERIVED_FROM_REQUEST
    assert EVENT_TYPE_SCHEMAS["tool_progress_event"].sensitivity is DERIVED_FROM_PROGRESS
    assert EVENT_TYPE_SCHEMAS["tool_call_completed"].sensitivity is DERIVED_FROM_RESULT


def test_schema_dataclass_is_frozen():
    """Anchor-2 durability discipline: schema rows are immutable."""
    schema = EVENT_TYPE_SCHEMAS["tool_call_dispatched"]
    with pytest.raises(Exception):
        schema.payload_kind = "signal"  # type: ignore[misc]


# --- retention_policy_id wiring to replay_privacy_policy.yaml -----------------

_YAML_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "companion_harness"
    / "replay_privacy_policy.yaml"
)


def _load_yaml_retention_ids() -> set[str]:
    with _YAML_PATH.open() as f:
        doc = yaml.safe_load(f)
    return set(doc["retention_policies"].keys())


def test_yaml_has_new_v0_1f_retention_ids():
    """Anchor 2: two new retention_policy_id values coined for v0.1f."""
    ids = _load_yaml_retention_ids()
    assert "tool_call_audit_30d" in ids
    assert "tool_result_default" in ids


def test_every_schema_retention_id_exists_in_yaml():
    """Anchor 2 + replay_privacy_policy.yaml entry-schema contract: every
    concrete retention_policy_id in the schema registry must exist in YAML."""
    yaml_ids = _load_yaml_retention_ids()
    for event_type, schema in EVENT_TYPE_SCHEMAS.items():
        if schema.retention_policy_id is not None:
            assert schema.retention_policy_id in yaml_ids, (
                f"{event_type} references unknown retention_policy_id "
                f"{schema.retention_policy_id!r} (not in YAML)"
            )


# --- progress_stage alphabet (Anchor 1) ---------------------------------------

def test_progress_stage_alphabet_locked():
    """Anchor 1: the 5-value alphabet is locked in tool_progress.ProgressStage."""
    from typing import get_args

    from companion_harness.tool_progress import ProgressStage

    assert set(get_args(ProgressStage)) == {
        "started",
        "scanning",
        "aggregating",
        "completed",
        "cancelled",
    }


# --- ToolProgressEvidence dataclass (Anchor 4) --------------------------------

def test_tool_progress_evidence_construction():
    """Sanity construction: instantiate with valid fields."""
    from companion_harness.tool_progress import ToolProgressEvidence

    evidence = ToolProgressEvidence(
        progress_stage="scanning",
        fillers_emitted_so_far=0,
        ms_since_last_filler=0,
        silence_won_already=False,
    )
    assert evidence.progress_stage == "scanning"
    assert evidence.fillers_emitted_so_far == 0
    assert evidence.ms_since_last_filler == 0
    assert evidence.silence_won_already is False


def test_tool_progress_evidence_is_frozen():
    """Frozen so the value can ride along on DecisionTrace inputs safely."""
    from companion_harness.tool_progress import ToolProgressEvidence

    evidence = ToolProgressEvidence(
        progress_stage="started",
        fillers_emitted_so_far=0,
        ms_since_last_filler=0,
        silence_won_already=False,
    )
    with pytest.raises(Exception):
        evidence.fillers_emitted_so_far = 1  # type: ignore[misc]


def test_tool_progress_evidence_is_hashable():
    """Frozen dataclass — should be hashable."""
    from companion_harness.tool_progress import ToolProgressEvidence

    evidence = ToolProgressEvidence(
        progress_stage="started",
        fillers_emitted_so_far=0,
        ms_since_last_filler=0,
        silence_won_already=False,
    )
    assert hash(evidence) == hash(evidence)


# --- PolicyInputs.tool_progress_evidence field (Anchor 4) ---------------------

def _minimal_policy_inputs_kwargs() -> dict:
    """Mirror existing call sites: only the non-default fields."""
    return dict(
        user_speaking=False,
        eou_probability=0.0,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="default",
        current_task_mode="default",
        social_mode="default",
        risk_mode="default",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


def test_policy_inputs_tool_progress_evidence_defaults_to_none():
    """Anchor 4 / Task 1: default None preserves zero blast radius on v0.1a–e."""
    from companion_harness.schemas import PolicyInputs

    inputs = PolicyInputs(**_minimal_policy_inputs_kwargs())
    assert inputs.tool_progress_evidence is None


def test_policy_inputs_accepts_tool_progress_evidence():
    """The field accepts a ToolProgressEvidence value (Task 5+ wiring use)."""
    from companion_harness.schemas import PolicyInputs
    from companion_harness.tool_progress import ToolProgressEvidence

    evidence = ToolProgressEvidence(
        progress_stage="scanning",
        fillers_emitted_so_far=1,
        ms_since_last_filler=2000,
        silence_won_already=True,
    )
    inputs = PolicyInputs(
        **_minimal_policy_inputs_kwargs(),
        tool_progress_evidence=evidence,
    )
    assert inputs.tool_progress_evidence is evidence
