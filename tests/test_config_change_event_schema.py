"""Tests for the config_change + operator_action event-type schema.

Phase 1 Task A of docs/design-config-and-dashboard.md §8 — adds two new
event types to the v0.1f event-schema registry, a shared retention policy
(`config_change_30d`) to replay_privacy_policy.yaml, and the new
`subject_class="operator"` value to the schema alphabet.

These tests validate ONLY the schema contract.  Downstream behavior
(ConfigStore singleton, /config/patch + /config/reset handlers, replay
wiring) is out of scope for Task A and lives in later Phase 1 / Phase 2
tasks per §9 of the design doc.
"""

from __future__ import annotations

import pathlib
from typing import get_args

import yaml

from companion_harness.schemas import Event
from companion_harness.v0_1f_event_schema import (
    EVENT_TYPE_SCHEMAS,
    SubjectClass,
    ToolEventSchema,
)


# --- registry contract --------------------------------------------------------

def test_config_change_event_type_registered() -> None:
    """§8 row 2: config_change schema lands in the v0.1f registry."""
    assert "config_change" in EVENT_TYPE_SCHEMAS


def test_operator_action_event_type_registered() -> None:
    """§8 row 1: operator_action schema lands in the v0.1f registry."""
    assert "operator_action" in EVENT_TYPE_SCHEMAS


def test_config_change_axes() -> None:
    """§8 row 2 verbatim axes."""
    schema = EVENT_TYPE_SCHEMAS["config_change"]
    assert isinstance(schema, ToolEventSchema)
    assert schema.payload_kind == "signal"
    assert schema.subject_class == "self"
    assert schema.sensitivity == "safe"
    assert schema.retention_policy_id == "config_change_30d"


def test_operator_action_axes() -> None:
    """§8 row 1 verbatim axes; subject_class is the new 'operator' value."""
    schema = EVENT_TYPE_SCHEMAS["operator_action"]
    assert isinstance(schema, ToolEventSchema)
    assert schema.payload_kind == "signal"
    assert schema.subject_class == "operator"
    assert schema.sensitivity == "safe"
    assert schema.retention_policy_id == "config_change_30d"


def test_config_change_required_fields() -> None:
    """§8 row 2: key / previous_value / new_value / applied_at_ms /
    operator_action_event_id."""
    schema = EVENT_TYPE_SCHEMAS["config_change"]
    assert set(schema.required_fields) == {
        "key",
        "previous_value",
        "new_value",
        "applied_at_ms",
        "operator_action_event_id",
    }


def test_operator_action_required_fields() -> None:
    """§8 row 1: endpoint / client_ip / request_id."""
    schema = EVENT_TYPE_SCHEMAS["operator_action"]
    assert set(schema.required_fields) == {"endpoint", "client_ip", "request_id"}


# --- subject_class alphabet (§11 OQ-4 resolution) -----------------------------

def test_subject_class_includes_operator() -> None:
    """OQ-4: 'operator' is added to the local v0.1f SubjectClass alphabet."""
    assert "operator" in set(get_args(SubjectClass))


def test_event_dataclass_accepts_operator_subject_class() -> None:
    """OQ-4: the canonical Event dataclass in schemas.py accepts the new
    'operator' value (the type alphabet is durable metadata; adding a new
    value flows through the Event envelope)."""
    evt = Event(
        event_id="op-1",
        session_id="sess-1",
        schema_version="v0.1f",
        seq_no=1,
        event_type="operator_action",
        timestamp_mono_ms=0,
        timestamp_wall="2026-05-15T00:00:00+00:00",
        source="manual_test_console.server",
        caused_by=[],
        payload_hash="abc123",
        payload_ref=None,
        payload_kind="signal",
        subject_class="operator",
        sensitivity="safe",
        retention_policy_id="config_change_30d",
    )
    assert evt.subject_class == "operator"


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


def test_config_change_30d_retention_policy_resolves() -> None:
    """The new retention_policy_id exists in replay_privacy_policy.yaml."""
    assert "config_change_30d" in _load_yaml_retention_ids()


def test_config_change_30d_retention_policy_ttl() -> None:
    """ttl_days_or_indefinite = 30 per the design doc."""
    with _YAML_PATH.open() as f:
        doc = yaml.safe_load(f)
    assert doc["retention_policies"]["config_change_30d"]["ttl_days_or_indefinite"] == 30


# --- sample payload round-trip through the schema validator ------------------
#
# "Sample payloads round-trip through the schema validator" — the schema
# validator for v0.1f events is the dataclass Event constructor itself (the
# registry locks the four classification axes; the dataclass enforces them
# via Literal typing).  We instantiate one Event per new event_type with
# every required field present and assert the round-trip yields the same
# field values.

def _make_event(
    *,
    event_type: str,
    subject_class: str,
    payload_inline: dict,
) -> Event:
    schema = EVENT_TYPE_SCHEMAS[event_type]
    return Event(
        event_id=f"{event_type}-event-1",
        session_id="sess-config-task-a",
        schema_version="v0.1f",
        seq_no=1,
        event_type=event_type,
        timestamp_mono_ms=12345,
        timestamp_wall="2026-05-15T14:32:00+00:00",
        source="manual_test_console.server",
        caused_by=[],
        payload_hash="hash-abc",
        payload_ref=None,
        payload_kind=schema.payload_kind,
        subject_class=subject_class,  # type: ignore[arg-type]
        sensitivity=schema.sensitivity,  # type: ignore[arg-type]
        retention_policy_id=schema.retention_policy_id,  # type: ignore[arg-type]
        payload_inline=payload_inline,
    )


def test_operator_action_sample_payload_roundtrip() -> None:
    """Sample operator_action carries §8-row-1 payload fields under the
    schema's locked axes."""
    payload = {
        "endpoint": "/config/patch",
        "client_ip": "127.0.0.1",
        "request_id": "req-7",
    }
    evt = _make_event(
        event_type="operator_action",
        subject_class="operator",
        payload_inline=payload,
    )
    assert evt.event_type == "operator_action"
    assert evt.subject_class == "operator"
    assert evt.payload_kind == "signal"
    assert evt.sensitivity == "safe"
    assert evt.retention_policy_id == "config_change_30d"
    assert evt.payload_inline == payload
    # required fields present in the sample payload
    for required in EVENT_TYPE_SCHEMAS["operator_action"].required_fields:
        assert required in payload


def test_config_change_sample_payload_roundtrip() -> None:
    """Sample config_change carries §8-row-2 payload fields under the
    schema's locked axes."""
    payload = {
        "key": "hard_cancel_after_ms",
        "previous_value": 120,
        "new_value": 150,
        "applied_at_ms": 12345,
        "operator_action_event_id": "operator_action-event-1",
    }
    evt = _make_event(
        event_type="config_change",
        subject_class="self",
        payload_inline=payload,
    )
    assert evt.event_type == "config_change"
    assert evt.subject_class == "self"
    assert evt.payload_kind == "signal"
    assert evt.sensitivity == "safe"
    assert evt.retention_policy_id == "config_change_30d"
    assert evt.payload_inline == payload
    # required fields present in the sample payload
    for required in EVENT_TYPE_SCHEMAS["config_change"].required_fields:
        assert required in payload
