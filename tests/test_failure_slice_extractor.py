"""Tests for CausalFailureSliceExtractor (Phase B2)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from companion_harness.evals.failure_slice_extractor import CausalFailureSliceExtractor
from companion_harness.evals.protocols import FailureSliceExtractor
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice
from companion_harness.schemas import EvaluationCase, ReplayRun


def _make_case(case_id: str = "fdb-test-0001") -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        stage=2,
        scenario="test_scenario",
        modalities=["audio"],
        fixture_ref="synthetic/v1/case.json",
        expected_events=["policy_decision"],
        expected_metrics={},
        consent_class="safe_eval_fixture",
        benchmark_name="full_duplex_bench",
        benchmark_version="v1",
        inputs={"synthetic": True},
        expected_behavior={},
    )


def _make_result(case_id: str, pass_: bool) -> BenchmarkResult:
    return BenchmarkResult(case_id=case_id, pass_=pass_, metrics=(), final_status="completed" if pass_ else "error")


def _write_event_log(events: list[dict]) -> Path:
    tf = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False, mode="w", encoding="utf-8")
    for evt in events:
        tf.write(json.dumps(evt) + "\n")
    tf.close()
    return Path(tf.name)


def _make_replay_run(case_id: str, event_log_path: Path | None = None) -> ReplayRun:
    return ReplayRun(
        run_id=f"test-run-{case_id}",
        case_id=case_id,
        implementation_config_version="test",
        policy_version="test",
        started_at="2026-05-15T00:00:00Z",
        finished_at="2026-05-15T00:00:01Z",
        results={},
        failures=[],
        event_log_path=event_log_path,
        timing_mode="synthetic_clock",
        final_status="error",
    )


def test_extract_returns_empty_list_for_passing_case() -> None:
    extractor = CausalFailureSliceExtractor()
    case = _make_case()
    result = _make_result(case.case_id, pass_=True)
    replay = _make_replay_run(case.case_id)
    slices = extractor.extract(case, replay, result)
    assert slices == []


def test_extract_walks_caused_by_backward() -> None:
    case = _make_case("fdb-walk-0001")
    events = [
        {
            "event_id": "bcs-fdb-walk-0001-start",
            "event_type": "benchmark_case_started",
            "source": "benchmark",
            "caused_by": [],
        },
        {
            "event_id": "policy-evt-001",
            "event_type": "policy_decision",
            "source": "speak_policy",
            "caused_by": ["bcs-fdb-walk-0001-start"],
            "payload_inline": {"action_type": "silence", "primary_reason_code": "SILENCE_WINS_TIE"},
        },
        {
            "event_id": "bcs-fdb-walk-0001-end",
            "event_type": "benchmark_case_completed",
            "source": "benchmark",
            "caused_by": ["policy-evt-001"],
        },
    ]
    log_path = _write_event_log(events)
    result = _make_result(case.case_id, pass_=False)
    replay = _make_replay_run(case.case_id, event_log_path=log_path)
    extractor = CausalFailureSliceExtractor()
    slices = extractor.extract(case, replay, result)
    assert len(slices) == 1
    fs = slices[0]
    assert fs.case_id == case.case_id
    # All three events should appear in causal subgraph
    assert "bcs-fdb-walk-0001-end" in fs.causal_event_ids
    assert "policy-evt-001" in fs.causal_event_ids
    assert "bcs-fdb-walk-0001-start" in fs.causal_event_ids


def test_extract_identifies_suspected_adapter() -> None:
    case = _make_case("fdb-adapter-0002")
    events = [
        {
            "event_id": "bcs-fdb-adapter-0002-end",
            "event_type": "benchmark_case_completed",
            "source": "benchmark",
            "caused_by": ["td-signal-001"],
        },
        {
            "event_id": "td-signal-001",
            "event_type": "turn_signal",
            "source": "turn_detector",
            "caused_by": [],
        },
    ]
    log_path = _write_event_log(events)
    result = _make_result(case.case_id, pass_=False)
    replay = _make_replay_run(case.case_id, event_log_path=log_path)
    extractor = CausalFailureSliceExtractor()
    slices = extractor.extract(case, replay, result)
    assert len(slices) == 1
    assert slices[0].suspected_adapter == "TurnDetectorSuite"


def test_extract_populates_causal_subgraph() -> None:
    case = _make_case("fdb-graph-0003")
    events = [
        {
            "event_id": "bcs-fdb-graph-0003-end",
            "event_type": "benchmark_case_completed",
            "source": "benchmark",
            "caused_by": ["sp-decision-001"],
        },
        {
            "event_id": "sp-decision-001",
            "event_type": "policy_decision",
            "source": "speak_policy",
            "caused_by": ["ts-001"],
            "payload_inline": {
                "action_type": "full_response",
                "primary_reason_code": "EOU_CONFIDENT",
                "eou_probability": 0.95,
                "policy_version": "v1",
            },
        },
        {
            "event_id": "ts-001",
            "event_type": "turn_signal",
            "source": "turn_detector",
            "caused_by": [],
        },
    ]
    log_path = _write_event_log(events)
    result = _make_result(case.case_id, pass_=False)
    replay = _make_replay_run(case.case_id, event_log_path=log_path)
    extractor = CausalFailureSliceExtractor()
    slices = extractor.extract(case, replay, result)
    assert len(slices) == 1
    fs = slices[0]
    # causal subgraph should include all 3 events
    assert len(fs.causal_event_ids) == 3
    # policy inputs extracted from payload_inline
    assert "action_type" in fs.relevant_policy_inputs
    assert fs.relevant_policy_inputs["action_type"] == "full_response"
    # suggested fix should reference SpeakPolicy
    assert fs.suggested_fix is not None
    assert "SpeakPolicy" in fs.suggested_fix


def test_extractor_satisfies_protocol() -> None:
    assert isinstance(CausalFailureSliceExtractor(), FailureSliceExtractor)


def test_extract_no_event_log_returns_single_slice_no_crash() -> None:
    case = _make_case("fdb-nolog-0004")
    result = _make_result(case.case_id, pass_=False)
    replay = _make_replay_run(case.case_id, event_log_path=None)
    extractor = CausalFailureSliceExtractor()
    slices = extractor.extract(case, replay, result)
    # Should return one slice with empty causal graph (no event log to walk)
    assert len(slices) == 1
    assert slices[0].causal_event_ids == ()
    assert slices[0].suspected_adapter is None
