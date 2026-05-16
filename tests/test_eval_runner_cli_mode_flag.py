"""v0.2c contract tests: CLI --mode / --limit surface (T6c).

Kept in a dedicated file so v0.2c CLI tests can be added or reverted atomically.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from companion_harness.evals.runners import main


# ---------------------------------------------------------------------------
# Dispatch tests — synthetic default, no HF call
# ---------------------------------------------------------------------------

def test_candor_adapter_dispatches():
    with tempfile.TemporaryDirectory() as tmpdir:
        rc = main(["run", "--adapter", "candor", "--output", tmpdir])
    assert rc == 0


def test_fdb_v1_adapter_dispatches():
    with tempfile.TemporaryDirectory() as tmpdir:
        rc = main(["run", "--adapter", "full_duplex_bench_v1", "--output", tmpdir])
    assert rc == 0


def test_fdb_v1_5_adapter_dispatches():
    with tempfile.TemporaryDirectory() as tmpdir:
        rc = main(["run", "--adapter", "full_duplex_bench_v1_5", "--output", tmpdir])
    assert rc == 0


def test_bogus_adapter_still_exits_2():
    rc = main(["run", "--adapter", "bogus_adapter_that_does_not_exist"])
    assert rc == 2


# ---------------------------------------------------------------------------
# --mode real without HF_TOKEN must return nonzero with actionable error
# ---------------------------------------------------------------------------

def test_mode_real_requires_hf_token(capsys):
    env = {k: v for k, v in os.environ.items() if k != "HF_TOKEN"}
    with patch.dict(os.environ, env, clear=True):
        with tempfile.TemporaryDirectory() as tmpdir:
            rc = main(["run", "--adapter", "candor", "--mode", "real", "--output", tmpdir])
    assert rc != 0
    captured = capsys.readouterr()
    assert "HF_TOKEN" in captured.err


# ---------------------------------------------------------------------------
# --limit caps cases
# ---------------------------------------------------------------------------

def test_limit_caps_cases():
    with tempfile.TemporaryDirectory() as tmpdir:
        rc = main(["run", "--adapter", "candor", "--mode", "synthetic", "--limit", "3", "--output", tmpdir])
        assert rc == 0
        # Find the run.json
        run_dirs = list(Path(tmpdir).iterdir())
        assert len(run_dirs) == 1
        run_json = run_dirs[0] / "run.json"
        data = json.loads(run_json.read_text())
    assert len(data["cases"]) == 3


# ---------------------------------------------------------------------------
# run.json carries mode, adapter, limit, skip_stats
# ---------------------------------------------------------------------------

def test_run_json_carries_mode_adapter_limit_skip_stats():
    with tempfile.TemporaryDirectory() as tmpdir:
        rc = main(["run", "--adapter", "candor", "--mode", "synthetic", "--limit", "5", "--output", tmpdir])
        assert rc == 0
        run_dirs = list(Path(tmpdir).iterdir())
        assert len(run_dirs) == 1
        data = json.loads((run_dirs[0] / "run.json").read_text())
    assert data["adapter"] == "candor"
    assert data["mode"] == "synthetic"
    assert data["limit"] == 5
    assert "skip_stats" in data
    assert "skipped_row_count" in data
    assert "total_row_count" in data
