"""Tests for the VoiceBench adapter (Phase D)."""

from __future__ import annotations

from companion_harness.evals.adapters.voicebench import (
    VoiceBenchCaseSource,
    build_voicebench,
)
from companion_harness.evals.protocols import BenchmarkAdapter, CaseSource
from companion_harness.schemas import EvaluationCase


_EXPECTED_TOTAL = 50  # 12 + 12 + 8 + 10 + 8


def test_case_source_yields_expected_count() -> None:
    src = VoiceBenchCaseSource(synthetic=True)
    cases = list(src.iter_cases("test"))
    assert len(cases) == _EXPECTED_TOTAL


def test_all_cases_are_evaluation_case() -> None:
    src = VoiceBenchCaseSource(synthetic=True)
    cases = list(src.iter_cases("test"))
    assert all(isinstance(c, EvaluationCase) for c in cases)


def test_case_ids_unique() -> None:
    src = VoiceBenchCaseSource(synthetic=True)
    ids = [c.case_id for c in src.iter_cases("test")]
    assert len(ids) == len(set(ids))


def test_synthetic_mode_is_default() -> None:
    src = VoiceBenchCaseSource()
    cases = list(src.iter_cases("test"))
    assert all(c.inputs is not None and c.inputs.get("synthetic") is True for c in cases)


def test_case_source_satisfies_protocol() -> None:
    assert isinstance(VoiceBenchCaseSource(), CaseSource)


def test_build_voicebench_returns_benchmark_adapter() -> None:
    adapter = build_voicebench()
    assert isinstance(adapter, BenchmarkAdapter)
    assert adapter.name == "voicebench"
    assert adapter.version == "v1"
    assert adapter.case_source is not None
    assert adapter.examiner is None


def test_real_mode_raises_not_implemented() -> None:
    import pytest
    src = VoiceBenchCaseSource(synthetic=False)
    with pytest.raises(NotImplementedError, match="datasets"):
        list(src.iter_cases("test"))
