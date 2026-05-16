"""Tests for scripts/v0_1f_replay_report.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parent.parent / "scripts" / "v0_1f_replay_report.py"


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
    assert report["case_id"] == "v0.1f_milestone"
    assert report["policy_version"] == "v0.1f"
    assert report["run_id"].startswith("v0.1f-")


def test_gate_table_contains_v0_1f_gates():
    """Gate table has all six new v0.1f gates."""
    from scripts.v0_1f_replay_report import _GATES

    gate_names = {g["gate"] for g in _GATES}
    expected = {
        "foreground_block_count_per_session",
        "tool_cancellation_latency_ms_p50",
        "filler_evidence_bound_compliance_rate",
        "tool_progress_attribution_rate",
        "filler_budget_compliance_rate",
        "tool_call_caused_by_closure_rate",
    }
    missing = expected - gate_names
    assert not missing, f"Missing v0.1f gates: {missing}"


def test_sentinel_present_passes(tmp_path, monkeypatch):
    """_check_manual_test_sentinel returns MET when sentinel is 0."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1f.md").write_text(
        "manual_test_critical_findings_open: 0\n\n## Findings\nNone.\n"
    )

    from scripts.v0_1f_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "MET"
    assert value == "manual_test_critical_findings_open: 0"


def test_sentinel_missing_fails(tmp_path, monkeypatch):
    """_check_manual_test_sentinel returns NOT_MEASURED when file is absent."""
    monkeypatch.chdir(tmp_path)

    from scripts.v0_1f_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "NOT_MEASURED"
    assert value is None


def test_sentinel_non_zero_fails(tmp_path, monkeypatch):
    """_check_manual_test_sentinel returns FAIL when sentinel is non-zero."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1f.md").write_text(
        "manual_test_critical_findings_open: 1\n\n## Findings\n- Critical: broken.\n"
    )

    from scripts.v0_1f_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "FAIL"
    assert "1" in (value or "")
