"""Tests for JSON + MD reporters (Task A4).

plan-eval-phase-a-execution.md Task A4 success criterion:
  pytest tests/test_eval_reporters.py -v → 4+ passed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from companion_harness.evals.reporters import JsonReporter, MarkdownReporter
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice, MetricValue

_RUN_ID = "test-run-001"

_METRIC_A = MetricValue(name="latency_ms", value=120.5, unit="ms", aggregation="scalar")
_METRIC_B = MetricValue(name="score", value={"p50": 0.9, "p95": 0.95}, unit=None, aggregation="distribution")

_RESULT_PASS = BenchmarkResult(
    case_id="thinking_pause",
    pass_=True,
    metrics=(_METRIC_A,),
    final_status="completed",
)
_RESULT_FAIL = BenchmarkResult(
    case_id="barge_in",
    pass_=False,
    metrics=(_METRIC_B,),
    final_status="completed",
)

_SLICE = FailureSlice(
    case_id="barge_in",
    causal_event_ids=("evt-1", "evt-2", "evt-3"),
    suspected_adapter="harness_native",
)


def test_json_reporter_writes_run_metrics_failure_slices(tmp_path: Path) -> None:
    reporter = JsonReporter(run_id=_RUN_ID, failure_slices=[_SLICE])
    reporter.render([_RESULT_PASS, _RESULT_FAIL], tmp_path)

    run_dir = tmp_path / _RUN_ID
    metrics_path = run_dir / "metrics.json"
    slices_path = run_dir / "failure_slices.json"

    assert metrics_path.exists(), "metrics.json not written"
    assert slices_path.exists(), "failure_slices.json not written"

    metrics = json.loads(metrics_path.read_text())
    assert isinstance(metrics, list)
    assert len(metrics) == 2
    case_ids = {m["case_id"] for m in metrics}
    assert "thinking_pause" in case_ids
    assert "barge_in" in case_ids
    # Verify sort_keys — keys in the first entry should be alphabetically ordered
    first_keys = list(metrics[0].keys())
    assert first_keys == sorted(first_keys)

    slices = json.loads(slices_path.read_text())
    assert isinstance(slices, list)
    assert len(slices) == 1
    assert slices[0]["case_id"] == "barge_in"
    assert slices[0]["suspected_adapter"] == "harness_native"
    assert "evt-1" in slices[0]["causal_event_ids"]


def test_md_reporter_writes_report_md(tmp_path: Path) -> None:
    reporter = MarkdownReporter(
        run_id=_RUN_ID,
        benchmark_name="harness_native",
        benchmark_version="0.1",
        run_timestamp="2026-05-15T00:00:00+00:00",
        failure_slices=[_SLICE],
    )
    reporter.render([_RESULT_PASS, _RESULT_FAIL], tmp_path)

    report_path = tmp_path / _RUN_ID / "report.md"
    assert report_path.exists(), "report.md not written"

    content = report_path.read_text()
    assert "# Eval run test-run-001" in content
    assert "## Summary" in content
    assert "## Cases" in content
    assert "2026-05-15T00:00:00+00:00" in content
    assert "harness_native" in content


def test_md_reporter_includes_per_case_pass_fail(tmp_path: Path) -> None:
    reporter = MarkdownReporter(run_id=_RUN_ID, failure_slices=[_SLICE])
    reporter.render([_RESULT_PASS, _RESULT_FAIL], tmp_path)

    content = (tmp_path / _RUN_ID / "report.md").read_text()

    assert "thinking_pause" in content
    assert "PASS" in content
    assert "barge_in" in content
    assert "FAIL" in content
    # Metrics table header
    assert "| name | value | unit | aggregation |" in content
    # Distribution value rendered as JSON
    assert "p50" in content


def test_md_reporter_links_event_log_paths(tmp_path: Path) -> None:
    reporter = MarkdownReporter(run_id=_RUN_ID)
    reporter.render([_RESULT_PASS, _RESULT_FAIL], tmp_path)

    content = (tmp_path / _RUN_ID / "report.md").read_text()

    assert "[event log](event_logs/thinking_pause.jsonl)" in content
    assert "[event log](event_logs/barge_in.jsonl)" in content
