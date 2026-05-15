"""Tests for scripts/v0_1h_replay_report.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


_SCRIPT = Path(__file__).parent.parent / "scripts" / "v0_1h_replay_report.py"
_FINDINGS_PATH = Path("docs/manual-test-findings-v0_1h.md")


def test_script_executable():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_script_reads_sentinel_when_present(tmp_path, monkeypatch):
    findings = tmp_path / "manual-test-findings-v0_1h.md"
    findings.write_text("manual_test_critical_findings_open: 0\n\n## Findings\nNone.\n")

    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1h.md").write_text(
        "manual_test_critical_findings_open: 0\n\n## Findings\nNone.\n"
    )

    from scripts.v0_1h_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "MET"
    assert value == "manual_test_critical_findings_open: 0"


def test_script_fails_when_sentinel_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1h.md").write_text(
        "## Findings\nNo sentinel here.\n"
    )

    from scripts.v0_1h_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "FAIL"
    assert value is not None
    assert "not found" in value


def test_script_fails_when_sentinel_non_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1h.md").write_text(
        "manual_test_critical_findings_open: 3\n\n## Findings\n- Critical: something broke.\n"
    )

    from scripts.v0_1h_replay_report import _check_manual_test_sentinel
    status, value = _check_manual_test_sentinel()
    assert status == "FAIL"
    assert "3" in (value or "")
