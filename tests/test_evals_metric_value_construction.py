"""MetricValue construction tests for all 3 aggregation literals.

Moved from A1 test plan per plan-eval-phase-a-execution.md Finding 1
(MetricValue lives in companion_harness/evals/schemas.py, not main schemas.py).
"""

import pytest

from companion_harness.evals.schemas import MetricValue


def test_metric_value_scalar():
    mv = MetricValue(name="latency_ms", value=123.4, unit="ms", aggregation="scalar")
    assert mv.name == "latency_ms"
    assert mv.value == 123.4
    assert mv.unit == "ms"
    assert mv.aggregation == "scalar"


def test_metric_value_histogram():
    mv = MetricValue(name="response_times", value={"p50": 100, "p95": 200}, unit="ms", aggregation="histogram")
    assert mv.aggregation == "histogram"
    assert isinstance(mv.value, dict)


def test_metric_value_distribution():
    mv = MetricValue(name="score_dist", value={"mean": 0.8, "std": 0.1}, unit=None, aggregation="distribution")
    assert mv.aggregation == "distribution"
    assert mv.unit is None


def test_metric_value_is_frozen():
    mv = MetricValue(name="x", value=1, unit=None, aggregation="scalar")
    with pytest.raises((AttributeError, TypeError)):
        mv.name = "y"  # type: ignore[misc]
