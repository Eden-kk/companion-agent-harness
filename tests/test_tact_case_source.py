"""PR1 — TactCaseSource loads the vendored TACT-Bench scenario set.

Hermetic: reads the vendored companion_harness/evals/adapters/tact_bench_data/
scenarios.yaml; no model, no network, no tact-bench checkout.
"""
from collections import Counter

from companion_harness.evals.adapters.tact_bench import TactCaseSource
from companion_harness.evals.protocols import CaseSource
from companion_harness.schemas import EvaluationCase


def test_tact_case_source_satisfies_protocol_and_loads_all_cases():
    src = TactCaseSource()
    assert isinstance(src, CaseSource)  # name/version + iter_cases present
    cases = list(src.iter_cases("all"))
    assert len(cases) == 14
    assert all(isinstance(c, EvaluationCase) for c in cases)
    ids = {c.case_id for c in cases}
    assert {"TC1-defer", "TC12-form", "TC13-form-full", "TC14-form-verbose-trap"} <= ids


def test_tact_case_source_behavior_distribution():
    cases = list(TactCaseSource().iter_cases("all"))
    by_behavior = Counter(c.expected_behavior["behavior"] for c in cases)
    # PR6 added two FORM cases (FULL-correct + verbose-trap).
    assert by_behavior == {"DEFER": 3, "DROP": 3, "DELIVER_NOW": 3, "FORM": 5}


def test_tact_case_source_carries_expected_form():
    cases = {c.case_id: c for c in TactCaseSource().iter_cases("all")}
    assert cases["TC13-form-full"].expected_behavior["expected_form"] == "FULL"
    assert cases["TC14-form-verbose-trap"].expected_behavior["expected_form"] == "BRIEF"
    assert cases["TC4-form"].expected_behavior["expected_form"] is None  # defaults to BRIEF in metric


def test_tact_case_source_carries_exposes_timing_and_inputs():
    cases = {c.case_id: c for c in TactCaseSource().iter_cases("all")}
    tc1 = cases["TC1-defer"]
    assert tc1.benchmark_name == "tact_bench"
    assert tc1.benchmark_version == "v1"
    assert tc1.expected_behavior["behavior"] == "DEFER"
    assert "breakpoint-hit" in tc1.expected_behavior["exposes"]
    assert tc1.expected_behavior["t_available"] == 3
    assert tc1.inputs["item"]["payload"]
    assert tc1.inputs["user_script"]  # non-empty


def test_tact_case_source_records_input_mode():
    text_cases = list(TactCaseSource(input_mode="text").iter_cases("all"))
    assert text_cases[0].modalities == ["text"]
    assert text_cases[0].inputs["input_mode"] == "text"
    audio_cases = list(TactCaseSource(input_mode="audio").iter_cases("all"))
    assert audio_cases[0].modalities == ["audio"]
