"""Tests for Full-Duplex-Bench v1 / v1.5 adapters (Phase B2)."""

from __future__ import annotations

from companion_harness.evals.adapters.full_duplex_bench import (
    FullDuplexBenchV1CaseSource,
    FullDuplexBenchV15CaseSource,
    build_v1,
    build_v1_5,
)
from companion_harness.evals.protocols import BenchmarkAdapter, CaseSource
from companion_harness.schemas import EvaluationCase


def test_v1_case_source_iterates() -> None:
    src = FullDuplexBenchV1CaseSource()
    cases = list(src.iter_cases("test"))
    assert len(cases) == 50
    assert all(isinstance(c, EvaluationCase) for c in cases)
    assert all(c.benchmark_version == "v1" for c in cases)


def test_v1_5_case_source_iterates() -> None:
    src = FullDuplexBenchV15CaseSource()
    cases = list(src.iter_cases("test"))
    assert len(cases) == 50
    assert all(isinstance(c, EvaluationCase) for c in cases)
    assert all(c.benchmark_version == "v1.5" for c in cases)


def test_fdb_synthetic_mode_default() -> None:
    src = FullDuplexBenchV1CaseSource()
    cases = list(src.iter_cases("test"))
    # All cases carry synthetic=True in inputs — confirms synthetic mode is on.
    assert all(c.inputs is not None and c.inputs.get("synthetic") is True for c in cases)


def test_v1_case_source_satisfies_protocol() -> None:
    assert isinstance(FullDuplexBenchV1CaseSource(), CaseSource)


def test_v1_5_case_source_satisfies_protocol() -> None:
    assert isinstance(FullDuplexBenchV15CaseSource(), CaseSource)


def test_build_v1_returns_benchmark_adapter() -> None:
    adapter = build_v1()
    assert isinstance(adapter, BenchmarkAdapter)
    assert adapter.name == "full_duplex_bench"
    assert adapter.version == "v1"
    assert adapter.case_source is not None


def test_build_v1_5_returns_benchmark_adapter() -> None:
    adapter = build_v1_5()
    assert isinstance(adapter, BenchmarkAdapter)
    assert adapter.name == "full_duplex_bench"
    assert adapter.version == "v1.5"
    assert adapter.case_source is not None


def test_v1_case_ids_unique() -> None:
    src = FullDuplexBenchV1CaseSource()
    ids = [c.case_id for c in src.iter_cases("test")]
    assert len(ids) == len(set(ids))


def test_v1_5_case_ids_unique() -> None:
    src = FullDuplexBenchV15CaseSource()
    ids = [c.case_id for c in src.iter_cases("test")]
    assert len(ids) == len(set(ids))
