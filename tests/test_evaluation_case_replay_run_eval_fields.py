"""Tests for Task A1 schema additions to EvaluationCase and ReplayRun.

Verifies that the 5 new Optional fields on EvaluationCase and the 6 new
Optional fields on ReplayRun (docs/plan-eval-phase-a-execution.md Task A1)
default correctly and do not break construction with the original 8 required
positional args.
"""
from pathlib import Path

from companion_harness.schemas import EvaluationCase, ReplayRun


# --- helpers ------------------------------------------------------------------

def _minimal_case() -> EvaluationCase:
    return EvaluationCase(
        case_id="c1",
        stage=1,
        scenario="s",
        modalities=["audio"],
        fixture_ref="f",
        expected_events=["e"],
        expected_metrics={},
        consent_class="safe",
    )


def _minimal_run() -> ReplayRun:
    return ReplayRun(
        run_id="r1",
        case_id="c1",
        implementation_config_version="v1",
        policy_version="v1",
        started_at="2026-01-01T00:00:00Z",
        finished_at=None,
        results={},
        failures=[],
    )


# --- EvaluationCase -----------------------------------------------------------

def test_evaluation_case_existing_fields_unchanged():
    case = _minimal_case()
    assert case.case_id == "c1"
    assert case.stage == 1
    assert case.scenario == "s"
    assert case.modalities == ["audio"]
    assert case.fixture_ref == "f"
    assert case.expected_events == ["e"]
    assert case.expected_metrics == {}
    assert case.consent_class == "safe"


def test_evaluation_case_new_optional_fields_default_none_or_empty():
    case = _minimal_case()
    assert case.benchmark_name is None
    assert case.benchmark_version is None
    assert case.inputs is None
    assert case.expected_behavior is None
    assert case.fixtures == []


def test_evaluation_case_full_construction():
    case = EvaluationCase(
        case_id="c2",
        stage=3,
        scenario="thinking_pause",
        modalities=["audio"],
        fixture_ref="thinking_pause/case.json",
        expected_events=["policy_decision"],
        expected_metrics={"latency_ms": 200},
        consent_class="safe_eval_fixture",
        benchmark_name="harness_native",
        benchmark_version="v1",
        inputs={"pytest_node": "tests/test_thinking_pause.py::test_thinking_pause"},
        expected_behavior={"pytest_status": "passed"},
        fixtures=["companion_harness/fixtures/thinking_pause_001/"],
    )
    assert case.benchmark_name == "harness_native"
    assert case.benchmark_version == "v1"
    assert case.inputs == {"pytest_node": "tests/test_thinking_pause.py::test_thinking_pause"}
    assert case.expected_behavior == {"pytest_status": "passed"}
    assert case.fixtures == ["companion_harness/fixtures/thinking_pause_001/"]


# --- ReplayRun ----------------------------------------------------------------

def test_replay_run_existing_fields_unchanged():
    run = _minimal_run()
    assert run.run_id == "r1"
    assert run.case_id == "c1"
    assert run.implementation_config_version == "v1"
    assert run.policy_version == "v1"
    assert run.started_at == "2026-01-01T00:00:00Z"
    assert run.finished_at is None
    assert run.results == {}
    assert run.failures == []


def test_replay_run_new_optional_fields_default_none_or_empty():
    run = _minimal_run()
    assert run.event_log_path is None
    assert run.decisions == []
    assert run.timing_mode is None
    assert run.started_at_mono_ms is None
    assert run.completed_at_mono_ms is None
    assert run.final_status is None


def test_replay_run_decisions_field_default_empty_list():
    run = _minimal_run()
    assert run.decisions == []
    assert isinstance(run.decisions, list)
    # Mutable default is via field(default_factory=list) — two instances are independent.
    run2 = _minimal_run()
    run.decisions.append(None)  # type: ignore[arg-type]
    assert run2.decisions == []


def test_replay_run_timing_mode_literal_accepts_both():
    run_s = ReplayRun(
        run_id="r2",
        case_id="c1",
        implementation_config_version="v1",
        policy_version="v1",
        started_at="2026-01-01T00:00:00Z",
        finished_at=None,
        results={},
        failures=[],
        timing_mode="synthetic_clock",
    )
    run_w = ReplayRun(
        run_id="r3",
        case_id="c1",
        implementation_config_version="v1",
        policy_version="v1",
        started_at="2026-01-01T00:00:00Z",
        finished_at=None,
        results={},
        failures=[],
        timing_mode="wall_clock",
    )
    assert run_s.timing_mode == "synthetic_clock"
    assert run_w.timing_mode == "wall_clock"
