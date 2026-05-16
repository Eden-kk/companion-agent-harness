"""Tests for distinct status_reason values in direct_question_latency (closes #107).

Three scenarios:
  1. trace_dir is None (not provided)     → "trace_dir_not_provided"
  2. trace_dir given but doesn't exist    → "trace_dir_missing"
  3. trace_dir exists, no full_response   → "no_full_response_decisions_in_session"
  4. trace_dir exists, full_response present → "measured" (status MET or NOT_MET or NOT_MEASURED
     due to sample count, NOT the config-error reasons)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from companion_harness.decision_trace_store import DecisionTraceStore
from companion_harness.live_loop_metrics import compute_metrics
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import DecisionTrace, Event


def _evt(
    event_type: str,
    t: int,
    *,
    event_id: str | None = None,
    caused_by: list[str] | None = None,
    payload_ref: str | None = None,
) -> Event:
    return Event(
        event_id=event_id or str(uuid.uuid4()),
        session_id="test-session",
        schema_version="0.1",
        seq_no=0,
        event_type=event_type,
        timestamp_mono_ms=t,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source="test",
        caused_by=caused_by or [],
        payload_hash="",
        payload_ref=payload_ref,
        payload_kind="signal",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="default",
    )


def _write_trace(trace_dir: Path, decision_id: str, action: str) -> str:
    trace = DecisionTrace(
        decision_id=decision_id,
        input_event_ids=[],
        signal_event_ids=[],
        threshold_path=["eou_threshold"],
        primary_reason_code=ReasonCode.EOU_CONFIRMED,
        supporting_reason_codes=[],
        counterfactuals={"action_selected": action},
        redacted_explanation=None,
        sensitive_explanation_ref=None,
        policy_version="0.1",
        config_version="test",
        model_adapter_versions={},
    )
    store = DecisionTraceStore(trace_dir)
    return store.write(trace)


_BASE_EVENTS = [
    _evt("harness_init", 0),
    _evt("orchestrator_started", 10),
]


def test_status_reason_trace_dir_missing(tmp_path: Path) -> None:
    """trace_dir path given but doesn't exist on disk → trace_dir_missing."""
    missing = tmp_path / "nonexistent_traces"
    assert not missing.exists()

    results = compute_metrics(_BASE_EVENTS, trace_dir=missing)
    dql = results["direct_question_latency"]

    assert dql.status == "NOT_MEASURED"
    assert dql.status_reason == "trace_dir_missing"


def test_status_reason_trace_dir_not_provided() -> None:
    """trace_dir is None (operator omitted --trace-dir and default didn't exist) → trace_dir_not_provided."""
    results = compute_metrics(_BASE_EVENTS, trace_dir=None)
    dql = results["direct_question_latency"]

    assert dql.status == "NOT_MEASURED"
    assert dql.status_reason == "trace_dir_not_provided"


def test_status_reason_no_full_response(tmp_path: Path) -> None:
    """trace_dir exists but all policy_decisions are non-full_response → no_full_response_decisions_in_session."""
    trace_dir = tmp_path / "decision_traces"
    pd_id = str(uuid.uuid4())
    ref = _write_trace(trace_dir, pd_id, "backchannel")

    events = list(_BASE_EVENTS) + [
        _evt("policy_decision", 100, event_id=pd_id, payload_ref=ref),
        _evt("assistant_audio_buffer_flushed", 400, caused_by=[pd_id]),
    ]
    results = compute_metrics(events, trace_dir=trace_dir)
    dql = results["direct_question_latency"]

    assert dql.status == "NOT_MEASURED"
    assert dql.status_reason == "no_full_response_decisions_in_session"


def test_status_reason_measured(tmp_path: Path) -> None:
    """trace_dir exists with full_response decisions → status_reason is NOT a config-error value."""
    trace_dir = tmp_path / "decision_traces"
    t = 100
    events = list(_BASE_EVENTS)
    for latency in [400, 500, 600, 700]:
        pd_id = str(uuid.uuid4())
        flush_id = str(uuid.uuid4())
        ref = _write_trace(trace_dir, pd_id, "full_response")
        events.append(_evt("policy_decision", t, event_id=pd_id, payload_ref=ref))
        events.append(_evt("assistant_audio_buffer_flushed", t + latency, caused_by=[pd_id], event_id=flush_id))
        t += latency + 300

    results = compute_metrics(events, trace_dir=trace_dir)
    dql = results["direct_question_latency"]

    assert dql.status_reason not in ("trace_dir_not_provided", "trace_dir_missing", "no_full_response_decisions_in_session")
    assert dql.sample_count == 4
    assert dql.status in ("MET", "NOT_MET")
