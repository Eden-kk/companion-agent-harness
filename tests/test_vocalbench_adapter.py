"""Tests for VocalBench adapter (Eval Phase D)."""

from __future__ import annotations

import pytest

from companion_harness.evals.adapters.vocalbench import (
    VocalBenchCaseSource,
    build_vocalbench,
)
from companion_harness.evals.protocols import BenchmarkAdapter, CaseSource
from companion_harness.schemas import EvaluationCase


def test_case_source_iterates_fifty_cases() -> None:
    src = VocalBenchCaseSource()
    cases = list(src.iter_cases("test"))
    assert len(cases) == 50
    assert all(isinstance(c, EvaluationCase) for c in cases)


def test_synthetic_mode_default() -> None:
    src = VocalBenchCaseSource()
    cases = list(src.iter_cases("test"))
    assert all(c.inputs is not None and c.inputs.get("synthetic") is True for c in cases)


def test_case_source_satisfies_protocol() -> None:
    assert isinstance(VocalBenchCaseSource(), CaseSource)


def test_case_ids_unique() -> None:
    src = VocalBenchCaseSource()
    ids = [c.case_id for c in src.iter_cases("test")]
    assert len(ids) == len(set(ids))


def test_build_vocalbench_returns_adapter() -> None:
    adapter = build_vocalbench()
    assert isinstance(adapter, BenchmarkAdapter)
    assert adapter.name == "vocalbench"
    assert adapter.version == "v1"
    assert adapter.case_source is not None
    assert adapter.examiner is None


def test_real_mode_raises_not_implemented() -> None:
    src = VocalBenchCaseSource(synthetic=False)
    with pytest.raises(NotImplementedError):
        list(src.iter_cases("test"))
