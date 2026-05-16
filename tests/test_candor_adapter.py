"""Phase B1 contract tests for the CANDOR distributional adapter.

test_candor_case_source_iterates       — CandorCaseSource yields 100 cases
test_distributional_metrics_compute    — all 4 metrics compute on a ReplayRun
test_distributional_md_reporter_renders — MD report written + envelope verdict present
test_synthetic_mode_default            — adapter.build() defaults to synthetic=True
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from companion_harness.evals.adapters.candor import (
    CandorCaseSource,
    CandorScenarioDriver,
    build,
)
from companion_harness.evals.metrics.distributional import (
    ALL_DISTRIBUTIONAL_METRICS,
    BackchannelPauseDistribution,
    OverlapMsDistribution,
    ResponseDelayDistribution,
    TurnGapMsDistribution,
)
from companion_harness.evals.reporters.distributional_md_reporter import DistributionalMdReporter
from companion_harness.evals.schemas import BenchmarkResult, MetricValue
from companion_harness.schemas import EvaluationCase, ReplayRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_replay_run(turn_gap=200.0, overlap=150.0, bc_pause=100.0, resp_delay=300.0) -> ReplayRun:
    return ReplayRun(
        run_id="test-run",
        case_id="test-case",
        implementation_config_version="test",
        policy_version="test",
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:01Z",
        results={
            "turn_gap_ms_observations":         [turn_gap],
            "overlap_ms_observations":           [overlap],
            "backchannel_pause_ms_observations": [bc_pause],
            "response_delay_ms_observations":    [resp_delay],
        },
        failures=[],
        timing_mode="synthetic_clock",
        final_status="completed",
    )


def _make_benchmark_result(metrics: tuple[MetricValue, ...]) -> BenchmarkResult:
    return BenchmarkResult(
        case_id="test-case",
        pass_=True,
        metrics=metrics,
        final_status="completed",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_candor_case_source_iterates():
    source = CandorCaseSource(synthetic=True, seed=42)
    cases = list(source.iter_cases(split="test"))
    assert len(cases) == 100
    first = cases[0]
    assert first.case_id == "candor_synthetic_test_000"
    assert first.benchmark_name == "candor"
    assert first.inputs is not None
    assert first.inputs["synthetic"] is True
    # All cases carry the four timing keys
    for case in cases:
        for key in ("turn_gap_ms", "overlap_ms", "backchannel_pause_ms", "response_delay_ms"):
            assert key in case.inputs, f"missing {key} in case {case.case_id}"
            assert isinstance(case.inputs[key], float)


def test_distributional_metrics_compute():
    rr = _make_replay_run(turn_gap=210.0, overlap=130.0, bc_pause=95.0, resp_delay=280.0)

    for metric in ALL_DISTRIBUTIONAL_METRICS:
        mv = metric.compute(rr)
        assert isinstance(mv, MetricValue)
        assert mv.aggregation == "distribution"
        assert mv.unit == "ms"
        assert isinstance(mv.value, dict)
        assert mv.value["count"] == 1
        assert mv.value["mean_ms"] is not None

    # Per-metric spot checks
    gap_mv = TurnGapMsDistribution().compute(rr)
    assert gap_mv.name == "turn_gap_ms_distribution"
    assert gap_mv.value["p50_ms"] == 210.0

    overlap_mv = OverlapMsDistribution().compute(rr)
    assert overlap_mv.name == "overlap_ms_distribution"

    bc_mv = BackchannelPauseDistribution().compute(rr)
    assert bc_mv.name == "backchannel_pause_distribution"

    resp_mv = ResponseDelayDistribution().compute(rr)
    assert resp_mv.name == "response_delay_distribution"


def test_distributional_md_reporter_renders():
    rr = _make_replay_run()
    metrics = tuple(m.compute(rr) for m in ALL_DISTRIBUTIONAL_METRICS)
    result = _make_benchmark_result(metrics)

    with tempfile.TemporaryDirectory() as tmpdir:
        out = Path(tmpdir)
        reporter = DistributionalMdReporter()
        reporter.render([result], out)

        report_path = out / "distributional_report.md"
        assert report_path.exists()
        text = report_path.read_text()

    assert "CANDOR Distributional Probe" in text
    assert "Framing" in text
    assert "human-ish envelope" in text
    assert "Turn gap" in text
    assert "Overlap" in text
    assert "Backchannel pause" in text
    assert "Response delay" in text
    # Envelope verdict present
    assert "Envelope verdict" in text
    # Human envelope numbers cited
    assert "200" in text  # human median for turn_gap
    # Histogram bars present
    assert "█" in text


def test_synthetic_mode_default():
    adapter = build()
    assert adapter.name == "candor"
    assert adapter.version == "v1"
    assert adapter.case_source is not None
    assert adapter.case_source.synthetic is True  # type: ignore[attr-defined]
    assert adapter.examiner is None
    assert len(adapter.metrics) == 4
    metric_names = [m.name for m in adapter.metrics]
    assert "turn_gap_ms_distribution" in metric_names
    assert "overlap_ms_distribution" in metric_names
    assert "backchannel_pause_distribution" in metric_names
    assert "response_delay_distribution" in metric_names
    # reporters list includes DistributionalMdReporter
    assert len(adapter.reporters) >= 1
    from companion_harness.evals.reporters.distributional_md_reporter import DistributionalMdReporter
    assert any(isinstance(r, DistributionalMdReporter) for r in adapter.reporters)
