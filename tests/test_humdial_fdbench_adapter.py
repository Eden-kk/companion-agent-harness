"""Tests for HumDial-FDBench adapter (Eval Phase D)."""

from __future__ import annotations

import pytest

from companion_harness.evals.adapters.humdial_fdbench import (
    HumDialFDBenchCaseSource,
    build_humdial_fdbench,
)
from companion_harness.evals.protocols import BenchmarkAdapter, CaseSource
from companion_harness.schemas import EvaluationCase


def test_iter_cases_yields_synthetic_cases() -> None:
    src = HumDialFDBenchCaseSource()
    cases = list(src.iter_cases("test"))
    assert len(cases) > 0
    assert all(isinstance(c, EvaluationCase) for c in cases)
    assert all(c.inputs is not None and c.inputs.get("synthetic") is True for c in cases)


def test_iter_cases_total_count_matches_30() -> None:
    src = HumDialFDBenchCaseSource()
    cases = list(src.iter_cases("test"))
    assert len(cases) == 30


def test_iter_cases_covers_all_4_categories() -> None:
    src = HumDialFDBenchCaseSource()
    cases = list(src.iter_cases("test"))
    categories = {c.scenario for c in cases}
    assert categories == {
        "dialog_act_recognition",
        "humor_response",
        "turn_yielding",
        "interrupt_recovery",
    }


def test_real_mode_raises_not_implemented() -> None:
    src = HumDialFDBenchCaseSource(synthetic=False)
    with pytest.raises(NotImplementedError):
        list(src.iter_cases("test"))


def test_case_has_required_fields() -> None:
    src = HumDialFDBenchCaseSource()
    cases = list(src.iter_cases("test"))
    for c in cases:
        assert c.case_id
        assert c.scenario in {
            "dialog_act_recognition",
            "humor_response",
            "turn_yielding",
            "interrupt_recovery",
        }
        assert c.benchmark_name == "humdial_fdbench"
        assert c.benchmark_version == "v1"
        assert c.modalities == ["audio"]
        assert c.consent_class == "safe_eval_fixture"
        assert c.inputs is not None


def test_iter_cases_deterministic_across_runs() -> None:
    src1 = HumDialFDBenchCaseSource(seed=42)
    src2 = HumDialFDBenchCaseSource(seed=42)
    cases1 = list(src1.iter_cases("test"))
    cases2 = list(src2.iter_cases("test"))
    assert [c.case_id for c in cases1] == [c.case_id for c in cases2]
    assert [c.inputs for c in cases1] == [c.inputs for c in cases2]


def test_satisfies_protocol() -> None:
    assert isinstance(HumDialFDBenchCaseSource(), CaseSource)


def test_build_humdial_fdbench_returns_adapter() -> None:
    adapter = build_humdial_fdbench()
    assert isinstance(adapter, BenchmarkAdapter)
    assert adapter.name == "humdial_fdbench"
    assert adapter.version == "v1"
