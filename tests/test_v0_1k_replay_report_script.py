"""Tests for scripts/v0_1k_replay_report.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parent.parent / "scripts" / "v0_1k_replay_report.py"


def test_script_executable():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_script_emits_json_with_required_sentinels(tmp_path, monkeypatch):
    """--json-only emits a JSON object with run_id, case_id, and policy_version."""
    monkeypatch.chdir(tmp_path)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--json-only"],
        capture_output=True,
        text=True,
    )
    stdout = result.stdout.strip()
    assert stdout, f"No stdout from script; stderr: {result.stderr[:500]}"

    report = json.loads(stdout)
    assert report["case_id"] == "v0.1k_milestone"
    assert report["policy_version"] == "v0.1k"
    assert report["run_id"].startswith("v0.1k-")


def test_gate_table_contains_v0_2b_gates():
    """Gate table has all new v0.2b gates."""
    from scripts.v0_1k_replay_report import _GATES

    gate_names = {g["gate"] for g in _GATES}
    expected = {
        "diarization_adapter_protocol_pass",
        "diarization_events_caused_by_closure_rate",
        "speaker_continuity_tie_breaker_pass",
        "policy_replay_exact_v0_1k",
    }
    missing = expected - gate_names
    assert not missing, f"Missing v0.2b gates: {missing}"


def test_gate_table_carries_all_v0_1j_gates():
    """All key v0.1j gates are present in the v0.1k table."""
    from scripts.v0_1k_replay_report import _GATES

    gate_names = {g["gate"] for g in _GATES}
    v0_1j_gates = {
        "stub_constant_count_in_realtime_path",
        "unavailable_marker_with_open_issue_rate",
        "eou_native_duplex_first_rate",
        "addressing_native_classifier_first_rate",
    }
    missing = v0_1j_gates - gate_names
    assert not missing, f"Missing carried v0.1j gates: {missing}"


def test_sentinel_met_when_file_present(tmp_path, monkeypatch):
    """_check_manual_test_sentinel returns MET when sentinel is 0."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1k.md").write_text(
        "manual_test_critical_findings_open: 0\n\n## Findings\nNone.\n"
    )

    from scripts.v0_1k_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "MET"
    assert value == "manual_test_critical_findings_open: 0"


def test_sentinel_not_measured_when_file_absent(tmp_path, monkeypatch):
    """_check_manual_test_sentinel returns NOT_MEASURED when file is absent."""
    monkeypatch.chdir(tmp_path)

    from scripts.v0_1k_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "NOT_MEASURED"
    assert value is None
