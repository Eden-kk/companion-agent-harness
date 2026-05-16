"""Unit tests for LiveExaminerCaseSource (T1 success criterion)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from companion_harness.evals.adapters.live_examiner import LiveExaminerCaseSource
from companion_harness.evals.protocols import CaseSource
from companion_harness.speak_policy import POLICY_VERSION


def _make_session(root: Path, session_id: str, utterances: list[dict]) -> Path:
    session_dir = root / session_id
    session_dir.mkdir()
    manifest = {
        "session_id": session_id,
        "schema_version": "1.0",
        "recording_date": "2026-05-16",
        "speaker_count": 2,
        "duration_ms": 10000,
        "license": "safe_eval_fixture",
        "redaction_notes": "synthetic test fixture",
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    gt = {"schema_version": "1.0", "session_id": session_id, "utterances": utterances}
    (session_dir / "ground_truth_speakers.json").write_text(json.dumps(gt), encoding="utf-8")
    return session_dir


def test_iter_cases_yields_one_case_per_utterance(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "utt_0", "t_start_ms": 0, "t_end_ms": 3000, "speaker_id": "speaker_A", "addressed_agent": True},
        {"utterance_id": "utt_1", "t_start_ms": 4000, "t_end_ms": 7000, "speaker_id": "speaker_B", "addressed_agent": False},
    ]
    _make_session(tmp_path, "test_session_001", utterances)
    src = LiveExaminerCaseSource(fixtures_root=tmp_path)
    cases = list(src.iter_cases("test"))
    assert len(cases) == 2
    assert cases[0].case_id == "test_session_001_utt_0"
    assert cases[1].case_id == "test_session_001_utt_1"


def test_case_expected_metrics_contents(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "utt_0", "t_start_ms": 0, "t_end_ms": 3000, "speaker_id": "speaker_A", "addressed_agent": True},
    ]
    _make_session(tmp_path, "test_session_002", utterances)
    src = LiveExaminerCaseSource(fixtures_root=tmp_path)
    cases = list(src.iter_cases("test"))
    assert len(cases) == 1
    case = cases[0]
    assert case.expected_metrics["addressed_agent"] is True
    assert case.expected_metrics["speaker_id"] == "speaker_A"


def test_case_benchmark_version_matches_policy_version(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "utt_0", "t_start_ms": 0, "t_end_ms": 3000, "speaker_id": "speaker_A", "addressed_agent": True},
    ]
    _make_session(tmp_path, "test_session_003", utterances)
    src = LiveExaminerCaseSource(fixtures_root=tmp_path)
    cases = list(src.iter_cases("test"))
    assert cases[0].benchmark_version == POLICY_VERSION


def test_real_mode_raises_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        LiveExaminerCaseSource(mode="real")


def test_invalid_mode_raises_value_error() -> None:
    with pytest.raises(ValueError):
        LiveExaminerCaseSource(mode="invalid")


def test_satisfies_case_source_protocol() -> None:
    src = LiveExaminerCaseSource()
    assert isinstance(src, CaseSource)
    assert src.name == "live_examiner_phase_c"
    assert src.version == "0.2"


def test_empty_fixtures_root_yields_no_cases(tmp_path: Path) -> None:
    src = LiveExaminerCaseSource(fixtures_root=tmp_path)
    assert list(src.iter_cases("test")) == []
