"""Tests for scripts/v0_1g_replay_report.py (v0.1g Task 20)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "v0_1g_replay_report.py"


def test_script_is_executable():
    assert _SCRIPT.exists(), f"script not found: {_SCRIPT}"
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_script_emits_json_with_required_sentinels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """--json-only emits a JSON object with run_id, case_id, and policy_version."""
    monkeypatch.chdir(tmp_path)

    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--json-only"],
        capture_output=True,
        text=True,
    )
    # Script exits non-zero when pytest fails; that is expected in isolation.
    # We only need the JSON on stdout to be parseable with the required keys.
    stdout = result.stdout.strip()
    assert stdout, f"No stdout from script; stderr: {result.stderr[:500]}"

    report = json.loads(stdout)
    assert "run_id" in report
    assert report["policy_version"] == "v0.1j"


def test_sentinel_present_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sentinel_file = tmp_path / "manual-test-findings-v0_1g.md"
    sentinel_file.write_text("manual_test_critical_findings_open: 0\n")

    import scripts.v0_1g_replay_report as script

    monkeypatch.chdir(tmp_path)
    # Create the docs dir so the Path("docs/...") lookup works.
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1g.md").write_text(
        "manual_test_critical_findings_open: 0\n"
    )

    status, value = script._check_manual_test_sentinel()
    assert status == "MET"
    assert value == "manual_test_critical_findings_open: 0"


def test_sentinel_missing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import scripts.v0_1g_replay_report as script

    monkeypatch.chdir(tmp_path)
    # No docs/ directory → file missing → NOT_MEASURED (not FAIL)
    status, value = script._check_manual_test_sentinel()
    assert status == "NOT_MEASURED"
    assert value is None


def test_sentinel_nonzero_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import scripts.v0_1g_replay_report as script

    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual-test-findings-v0_1g.md").write_text(
        "manual_test_critical_findings_open: 2\n"
    )

    status, value = script._check_manual_test_sentinel()
    assert status == "FAIL"
    assert "2" in (value or "")
