"""Canary: A2 skeleton is importable and --help exits 0.

Verifies all 6 Protocols + BenchmarkAdapter from evals/protocols.py and all
5 schema types from evals/schemas.py import cleanly (no broken import chain).
Also checks python -m companion_harness.evals --help exits 0.
"""

import subprocess
import sys

from companion_harness.evals.protocols import (
    BenchmarkAdapter,
    CaseSource,
    Examiner,
    FailureSliceExtractor,
    Metric,
    Reporter,
    ScenarioDriver,
)
from companion_harness.evals.schemas import (
    BenchmarkResult,
    BenchmarkSuiteManifest,
    FailureSlice,
    MetricValue,
    ReporterConfig,
)


def test_six_protocols_not_none():
    assert CaseSource is not None
    assert ScenarioDriver is not None
    assert Examiner is not None
    assert Metric is not None
    assert FailureSliceExtractor is not None
    assert Reporter is not None


def test_benchmark_adapter_not_none():
    assert BenchmarkAdapter is not None


def test_five_schema_types_not_none():
    assert MetricValue is not None
    assert BenchmarkResult is not None
    assert FailureSlice is not None
    assert BenchmarkSuiteManifest is not None
    assert ReporterConfig is not None


def test_help_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "companion_harness.evals", "--help"],
        capture_output=True,
    )
    assert result.returncode == 0
