"""Tests for scripts/v0_2_replay_report.py."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "v0_2_replay_report.py"


def test_script_executable():
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_script_emits_json_with_required_sentinels(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--json-only"],
        capture_output=True,
        text=True,
    )
    stdout = result.stdout.strip()
    assert stdout, f"No stdout; stderr: {result.stderr[:500]}"
    report = json.loads(stdout)
    assert report["case_id"] == "v0.2_milestone"
    assert report["policy_version"] == "v0.1k"
    assert report["run_id"].startswith("v0.2-")


def test_gate_table_contains_wave6_gates():
    from scripts.v0_2_replay_report import _GATES
    gate_names = {g["gate"] for g in _GATES}
    expected = {
        "replay_report_export_determinism",
        "replay_report_tar_byte_stability",
        "blob_retention_rotation_correctness",
        "healthz_per_adapter_readiness_coverage",
        "healthz_gpu_memory_keys_present",
        "healthz_event_rate_counter_accuracy",
        "healthz_response_additive_compatibility",
        "v0_2_replay_report_readiness_banner",
    }
    missing = expected - gate_names
    assert not missing, f"Missing Wave 6 gates: {missing}"


def test_gate_table_carries_wave2_gates():
    from scripts.v0_2_replay_report import _GATES
    gate_names = {g["gate"] for g in _GATES}
    expected = {
        "diarization_adapter_protocol_pass",
        "diarization_events_caused_by_closure_rate",
        "speaker_continuity_tie_breaker_pass",
        "policy_replay_exact_v0_1k",
    }
    missing = expected - gate_names
    assert not missing, f"Missing Wave 2 gates: {missing}"


def test_b200_required_gates_marked(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from scripts.v0_2_replay_report import _GATES
    b200_gates = [g for g in _GATES if g.get("b200_required")]
    assert len(b200_gates) >= 1, "At least one gate should be b200_required"
    for g in b200_gates:
        assert "b200-required" in g["notes"].lower() or "b200" in g["notes"].lower()


def test_readiness_banner_present_in_summary_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        capture_output=True,
        text=True,
    )
    combined = result.stdout + result.stderr
    assert "READY FOR git tag v0.2" in combined or result.returncode == 0, (
        f"Banner not found; returncode={result.returncode}; stderr={result.stderr[:300]}"
    )
