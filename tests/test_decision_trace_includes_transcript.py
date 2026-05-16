"""Tests for Finding 8 fix: user_transcript in DecisionTrace (SensitiveField).

Verifies:
- DecisionTrace has user_transcript and user_transcript_preview fields.
- build_decision_trace() populates them from PolicyInputs.user_transcript.
- user_transcript is wrapped in SensitiveField with correct retention_policy_id.
- Tier-B replay stays bit-identical post-add (determinism invariant #5).
- Empty/absent transcript yields None, not empty string.
- DecisionTraceStore redacts the transcript value on disk but preserves the wrapper.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from companion_harness.decision_trace_store import DecisionTraceStore
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import DecisionTrace, PolicyInputs, SensitiveField
from companion_harness.speak_policy import build_decision_trace, decide


def _minimal_inputs(**overrides) -> PolicyInputs:
    base = dict(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )
    base.update(overrides)
    return PolicyInputs(**base)


def test_decision_trace_has_user_transcript_field() -> None:
    """DecisionTrace dataclass has user_transcript and user_transcript_preview fields."""
    fields = {f.name for f in dataclasses.fields(DecisionTrace)}
    assert "user_transcript" in fields
    assert "user_transcript_preview" in fields


def test_user_transcript_populated_from_policy_inputs() -> None:
    """build_decision_trace() copies user_transcript from PolicyInputs."""
    inputs = _minimal_inputs(user_transcript="What is the weather today?")
    decision = decide(inputs, signal_event_ids=["sig-001"])
    trace = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-001"],
        decision_id="d-transcript-001",
    )

    assert trace.user_transcript is not None
    assert trace.user_transcript.value == "What is the weather today?"
    assert trace.user_transcript_preview == "What is the weather today?"


def test_user_transcript_wrapped_in_sensitive_field() -> None:
    """user_transcript is a SensitiveField with retention_policy_id = 'transcript_audit_30d'."""
    inputs = _minimal_inputs(user_transcript="Hello there")
    decision = decide(inputs, signal_event_ids=["sig-002"])
    trace = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-002"],
        decision_id="d-sf-001",
    )

    assert isinstance(trace.user_transcript, SensitiveField)
    assert trace.user_transcript.retention_policy_id == "transcript_audit_30d"
    assert trace.user_transcript.sensitivity == "sensitive"


def test_user_transcript_preview_truncated_at_50_chars() -> None:
    """user_transcript_preview is capped at the first 50 chars of the transcript."""
    long_text = "A" * 80
    inputs = _minimal_inputs(user_transcript=long_text)
    decision = decide(inputs, signal_event_ids=["sig-003"])
    trace = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-003"],
        decision_id="d-preview-001",
    )

    assert trace.user_transcript_preview == "A" * 50
    assert len(trace.user_transcript_preview) == 50


def test_empty_transcript_is_none_not_empty_string() -> None:
    """When user_transcript is empty, both fields are None (not empty string/SensitiveField)."""
    inputs = _minimal_inputs(user_transcript="")
    decision = decide(inputs, signal_event_ids=["sig-004"])
    trace = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-004"],
        decision_id="d-empty-001",
    )

    assert trace.user_transcript is None
    assert trace.user_transcript_preview is None


def test_decision_trace_replay_preserves_transcript() -> None:
    """Tier-B replay: two identical inputs yield bit-identical traces (invariant #5)."""
    inputs = _minimal_inputs(user_transcript="reproduce this")
    decision = decide(inputs, signal_event_ids=["sig-det-t"])

    trace1 = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-det-t"],
        decision_id="d-det-t",
    )
    trace2 = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-det-t"],
        decision_id="d-det-t",
    )

    assert dataclasses.astuple(trace1) == dataclasses.astuple(trace2)


def test_store_redacts_transcript_value_on_disk(tmp_path: Path) -> None:
    """DecisionTraceStore.write() nulls user_transcript.value on disk."""
    store = DecisionTraceStore(tmp_path)
    inputs = _minimal_inputs(user_transcript="private user speech")
    decision = decide(inputs, signal_event_ids=["sig-store-001"])
    trace = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-store-001"],
        decision_id="d-store-redact",
    )
    store.write(trace)

    raw = json.loads((tmp_path / "d-store-redact.json").read_text())
    assert raw["user_transcript"] is not None, "wrapper must remain on disk"
    assert raw["user_transcript"]["value"] is None, "raw value must be redacted"
    assert raw["user_transcript"]["value_ref"] is None
    assert raw["user_transcript"]["retention_policy_id"] == "transcript_audit_30d"
    # preview is non-sensitive and must survive on disk
    assert raw["user_transcript_preview"] == "private user speech"


def test_store_roundtrip_with_transcript(tmp_path: Path) -> None:
    """DecisionTraceStore read() reconstructs user_transcript as SensitiveField."""
    store = DecisionTraceStore(tmp_path)
    inputs = _minimal_inputs(user_transcript="round-trip text")
    decision = decide(inputs, signal_event_ids=["sig-rt-001"])
    trace = build_decision_trace(
        decision=decision,
        inputs=inputs,
        signal_event_ids=["sig-rt-001"],
        decision_id="d-roundtrip-transcript",
    )
    store.write(trace)
    recovered = store.read("d-roundtrip-transcript")

    # value is redacted on disk, so recovered.user_transcript.value is None
    assert isinstance(recovered.user_transcript, SensitiveField)
    assert recovered.user_transcript.retention_policy_id == "transcript_audit_30d"
    assert recovered.user_transcript.value is None
    assert recovered.user_transcript_preview == "round-trip text"
