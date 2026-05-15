"""Unit tests for DecisionTraceStore (v0.1e Task 4 Anchor 4 v6)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from companion_harness.decision_trace_store import DecisionTraceStore
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import DecisionTrace


def _minimal_trace(decision_id: str = "dec-001") -> DecisionTrace:
    return DecisionTrace(
        decision_id=decision_id,
        input_event_ids=["sig-001"],
        signal_event_ids=["sig-001"],
        threshold_path=["eou_gate:passed", "user_addressed_agent:full_response"],
        primary_reason_code=ReasonCode.EOU_CONFIRMED,
        supporting_reason_codes=[ReasonCode.USER_ADDRESSED_AGENT],
        counterfactuals={"action_selected": "full_response"},
        redacted_explanation=None,
        sensitive_explanation_ref=None,
        policy_version="v0.1d",
        config_version="v0.1e",
        model_adapter_versions={},
        retrieval_used=[],
    )


def test_write_then_read_roundtrip(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path)
    trace = _minimal_trace("dec-roundtrip")
    store.write(trace)
    recovered = store.read("dec-roundtrip")
    assert recovered.decision_id == trace.decision_id
    assert recovered.primary_reason_code == trace.primary_reason_code
    assert recovered.supporting_reason_codes == trace.supporting_reason_codes
    assert recovered.threshold_path == trace.threshold_path
    assert recovered.counterfactuals == trace.counterfactuals
    assert recovered.policy_version == trace.policy_version
    assert recovered.config_version == trace.config_version
    assert recovered.retrieval_used == trace.retrieval_used
    assert recovered.redacted_explanation is None
    assert recovered.sensitive_explanation_ref is None


def test_write_returns_correct_uri(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path)
    uri = store.write(_minimal_trace("dec-uri"))
    assert uri == "decision_trace://dec-uri"


def test_read_missing_raises_file_not_found(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path)
    with pytest.raises(FileNotFoundError):
        store.read("nonexistent-decision-id")


def test_atomic_rename_no_tmp_after_commit(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path)
    store.write(_minimal_trace("dec-atomic"))
    assert (tmp_path / "dec-atomic.json").exists()
    assert not (tmp_path / "dec-atomic.json.tmp").exists()


def test_mkdir_on_construct(tmp_path: Path) -> None:
    new_dir = tmp_path / "nested" / "traces"
    assert not new_dir.exists()
    DecisionTraceStore(new_dir)
    assert new_dir.is_dir()


def test_redacted_explanation_null_on_disk(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path)
    trace = DecisionTrace(
        decision_id="dec-redacted",
        input_event_ids=["sig-001"],
        signal_event_ids=["sig-001"],
        threshold_path=["eou_gate:passed"],
        primary_reason_code=ReasonCode.EOU_CONFIRMED,
        supporting_reason_codes=[],
        counterfactuals={"action_selected": "full_response"},
        redacted_explanation="This should not be on disk",
        sensitive_explanation_ref="ref://sensitive",
        policy_version="v0.1d",
        config_version="v0.1e",
        model_adapter_versions={},
        retrieval_used=[],
    )
    store.write(trace)
    raw = json.loads((tmp_path / "dec-redacted.json").read_text())
    assert raw["redacted_explanation"] is None
    assert raw["sensitive_explanation_ref"] is None


def test_duplicate_decision_id_silent_overwrite(tmp_path: Path) -> None:
    store = DecisionTraceStore(tmp_path)
    trace1 = _minimal_trace("dec-dup")
    store.write(trace1)

    trace2 = DecisionTrace(
        decision_id="dec-dup",
        input_event_ids=["sig-002"],
        signal_event_ids=["sig-002"],
        threshold_path=["silence:fallthrough"],
        primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
        supporting_reason_codes=[],
        counterfactuals={"action_selected": "silence"},
        redacted_explanation=None,
        sensitive_explanation_ref=None,
        policy_version="v0.1d",
        config_version="v0.1e",
        model_adapter_versions={},
        retrieval_used=[],
    )
    store.write(trace2)  # must not raise
    recovered = store.read("dec-dup")
    assert recovered.primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT
