"""Tests for the harness_native adapter (plan-eval-phase-a-execution.md Task A3)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from companion_harness.evals.adapters.harness_native import (
    HarnessNativePytestDriver,
    _HARNESS_NATIVE_CASES,
    build,
)
from companion_harness.evals.protocols import BenchmarkAdapter
from companion_harness.schemas import EvaluationCase


def test_harness_native_cases_count_is_4():
    assert len(_HARNESS_NATIVE_CASES) == 4


def test_each_case_has_required_positional_fields():
    required = [
        "case_id", "stage", "scenario", "modalities",
        "fixture_ref", "expected_events", "expected_metrics", "consent_class",
    ]
    for case in _HARNESS_NATIVE_CASES:
        assert isinstance(case, EvaluationCase)
        for field in required:
            assert getattr(case, field) is not None, (
                f"case {case.case_id!r}: required field {field!r} is None"
            )
        assert case.inputs is not None and "pytest_node" in case.inputs, (
            f"case {case.case_id!r}: inputs must contain 'pytest_node'"
        )


def test_subprocess_pytest_exit_0_maps_to_completed(tmp_path):
    driver = HarnessNativePytestDriver()
    case = _HARNESS_NATIVE_CASES[0]

    fake_proc = MagicMock()
    fake_proc.returncode = 0

    with patch("subprocess.run", return_value=fake_proc):
        replay_run = driver._run_sync(case, tmp_path)

    assert replay_run.final_status == "completed"
    assert replay_run.case_id == case.case_id
    assert replay_run.event_log_path is not None
    assert replay_run.event_log_path.exists()


def test_subprocess_pytest_exit_5_maps_to_skipped(tmp_path):
    driver = HarnessNativePytestDriver()
    case = _HARNESS_NATIVE_CASES[1]

    fake_proc = MagicMock()
    fake_proc.returncode = 5

    with patch("subprocess.run", return_value=fake_proc):
        replay_run = driver._run_sync(case, tmp_path)

    assert replay_run.final_status == "skipped"


def test_subprocess_pytest_exit_1_maps_to_error(tmp_path):
    driver = HarnessNativePytestDriver()
    case = _HARNESS_NATIVE_CASES[2]

    fake_proc = MagicMock()
    fake_proc.returncode = 1

    with patch("subprocess.run", return_value=fake_proc):
        replay_run = driver._run_sync(case, tmp_path)

    assert replay_run.final_status == "error"
