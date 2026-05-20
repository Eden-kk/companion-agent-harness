"""v0.2c contract tests: FDB real-mode loader (T6b).

Tests that require live HF access carry ``@pytest.mark.real_corpus`` and are
excluded from default ``pytest tests/`` per pyproject.toml marker config.
"""

from __future__ import annotations

import os
import sys
import types
from unittest.mock import patch

import pytest

from companion_harness.evals.adapters.full_duplex_bench import (
    FullDuplexBenchV1CaseSource,
    FullDuplexBenchV15CaseSource,
    _SCENARIOS_V1,
    _SCENARIOS_V1_5,
    _fdb_v1_row_to_evaluation_case,
    _fdb_v15_row_to_evaluation_case,
    build_v1,
    build_v1_5,
)
from companion_harness.evals.protocols import CaseSource
from companion_harness.schemas import EvaluationCase


# ---------------------------------------------------------------------------
# Regression: synthetic defaults unchanged
# ---------------------------------------------------------------------------

def test_v1_synthetic_default_unchanged():
    src = FullDuplexBenchV1CaseSource()
    cases = list(src.iter_cases("test"))
    assert len(cases) == 50
    assert all(c.inputs is not None and c.inputs.get("synthetic") is True for c in cases)


def test_v15_synthetic_default_unchanged():
    src = FullDuplexBenchV15CaseSource()
    cases = list(src.iter_cases("test"))
    assert len(cases) == 50
    assert all(c.inputs is not None and c.inputs.get("synthetic") is True for c in cases)


# ---------------------------------------------------------------------------
# Real-mode HF_TOKEN check
# ---------------------------------------------------------------------------

def _fake_dataset_info(*args, **kwargs):
    class _FakeInfo:
        card_data = {"license": "apache-2.0"}
    return _FakeInfo()


def test_fdb_v1_real_mode_requires_hf_token():
    env = {k: v for k, v in os.environ.items() if k != "HF_TOKEN"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError, match="HF_TOKEN missing"):
            FullDuplexBenchV1CaseSource(synthetic=False)


def test_fdb_v15_real_mode_requires_hf_token():
    env = {k: v for k, v in os.environ.items() if k != "HF_TOKEN"}
    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(RuntimeError, match="HF_TOKEN missing"):
            FullDuplexBenchV15CaseSource(synthetic=False)


# ---------------------------------------------------------------------------
# Live HF smoke tests (real_corpus marker)
# ---------------------------------------------------------------------------

@pytest.mark.real_corpus
def test_fdb_v1_real_mode_smoke_yields_cases():
    assert os.environ.get("HF_TOKEN"), "HF_TOKEN must be set for real_corpus tests"
    src = FullDuplexBenchV1CaseSource(synthetic=False)
    cases = []
    for i, c in enumerate(src.iter_cases("train")):
        cases.append(c)
        if i >= 4:
            break
    assert len(cases) >= 1
    assert all(isinstance(c, EvaluationCase) for c in cases)


@pytest.mark.real_corpus
def test_fdb_v15_real_mode_smoke_yields_cases():
    assert os.environ.get("HF_TOKEN"), "HF_TOKEN must be set for real_corpus tests"
    src = FullDuplexBenchV15CaseSource(synthetic=False)
    cases = []
    for i, c in enumerate(src.iter_cases("train")):
        cases.append(c)
        if i >= 4:
            break
    assert len(cases) >= 1
    assert all(isinstance(c, EvaluationCase) for c in cases)


# ---------------------------------------------------------------------------
# Mapper tests: known scenarios pass through, unknown scenarios raise
# ---------------------------------------------------------------------------

def _make_good_row(scenario: str) -> dict:
    return {
        "scenario": scenario,
        "audio": {"bytes": b"\x00\x01", "sampling_rate": 16000},
    }


def test_v1_mapper_handles_known_scenarios():
    for i, scenario in enumerate(_SCENARIOS_V1):
        row = _make_good_row(scenario)
        case = _fdb_v1_row_to_evaluation_case(row, i)
        assert isinstance(case, EvaluationCase)
        assert case.scenario == scenario
        assert case.benchmark_version == "v1"
        assert case.inputs is not None
        assert case.inputs["audio_pcm_bytes"] == b"\x00\x01"
        assert case.inputs["synthetic"] is False


def test_v15_mapper_handles_known_scenarios():
    for i, scenario in enumerate(_SCENARIOS_V1_5):
        row = _make_good_row(scenario)
        case = _fdb_v15_row_to_evaluation_case(row, i)
        assert isinstance(case, EvaluationCase)
        assert case.scenario == scenario
        assert case.benchmark_version == "v1.5"
        assert case.inputs is not None
        assert case.inputs["audio_pcm_bytes"] == b"\x00\x01"
        assert case.inputs["synthetic"] is False


def test_v1_mapper_rejects_unknown_scenario():
    row = _make_good_row("unknown_scenario_xyz")
    with pytest.raises(ValueError, match="unrecognized scenario"):
        _fdb_v1_row_to_evaluation_case(row, 0)


def test_v15_mapper_rejects_unknown_scenario():
    row = _make_good_row("not_a_real_scenario")
    with pytest.raises(ValueError, match="unrecognized scenario"):
        _fdb_v15_row_to_evaluation_case(row, 0)


# ---------------------------------------------------------------------------
# Real-mode skip counter (monkeypatched)
# ---------------------------------------------------------------------------

def test_fdb_v1_skip_counter_tracks_malformed_rows():
    pytest.importorskip("huggingface_hub")
    with patch("huggingface_hub.dataset_info", _fake_dataset_info):
        with patch.dict(os.environ, {"HF_TOKEN": "fake-token"}):
            src = FullDuplexBenchV1CaseSource(synthetic=False)

    good_row = _make_good_row(_SCENARIOS_V1[0])
    bad_row = {"scenario": _SCENARIOS_V1[0]}  # missing audio

    rows = [good_row] * 10 + [bad_row]

    def _fake_load_dataset(*args, **kwargs):
        return iter(rows)

    fake_datasets_mod = types.ModuleType("datasets")
    fake_datasets_mod.load_dataset = _fake_load_dataset  # type: ignore[attr-defined]
    sys.modules["datasets"] = fake_datasets_mod
    try:
        cases = list(src._real_cases("test"))
    finally:
        del sys.modules["datasets"]

    stats = src.skip_stats()
    assert stats["attempted"] == 11
    assert stats["skipped"] == 1
    assert len(cases) == 10


# ---------------------------------------------------------------------------
# Builder regression guards
# ---------------------------------------------------------------------------

def test_build_v1_with_synthetic_flag():
    adapter = build_v1(synthetic=True)
    assert adapter.case_source.synthetic is True  # type: ignore[attr-defined]


def test_build_v1_5_with_synthetic_flag():
    adapter = build_v1_5(synthetic=True)
    assert adapter.case_source.synthetic is True  # type: ignore[attr-defined]
