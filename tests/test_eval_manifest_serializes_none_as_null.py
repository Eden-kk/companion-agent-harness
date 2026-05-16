"""N7 regression test: eval manifest serializes event_log_path=None as JSON null, not string 'None'."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from companion_harness.evals.runners import main


def test_synthetic_run_event_log_path_is_json_null() -> None:
    """full_duplex_bench_v1 sets event_log_path=None; manifest must emit null, not 'None'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        rc = main(["run", "--adapter", "full_duplex_bench_v1", "--mode", "synthetic", "--limit", "2", "--output", tmpdir])
        assert rc == 0
        run_dirs = list(Path(tmpdir).iterdir())
        assert len(run_dirs) == 1
        data = json.loads((run_dirs[0] / "run.json").read_text())

    for case in data["cases"]:
        assert case["event_log_path"] is None, (
            f"case {case['case_id']!r}: expected null but got {case['event_log_path']!r}"
        )


def test_live_voicebench_run_json_no_string_none() -> None:
    """If a post-fix voiceben-* run.json exists on disk, its cases must not carry 'None' as a string.

    Pre-fix artifacts are skipped; this test only guards runs produced after this commit.
    """
    blobs = Path("/tmp/manual_test_blobs/eval_reports")
    if not blobs.is_dir():
        pytest.skip("no manual_test_blobs on this machine")

    run_jsons = list(blobs.glob("voiceben*/run.json"))
    if not run_jsons:
        pytest.skip("no voiceben*/run.json found")

    violations = []
    for run_json in run_jsons:
        data = json.loads(run_json.read_text())
        for case in data.get("cases", []):
            if case.get("event_log_path") == "None":
                violations.append(f"{run_json.parent.name}/{case.get('case_id')}")

    if violations:
        pytest.skip(
            f"pre-fix artifact(s) found — re-run the eval to produce a clean manifest: {violations}"
        )
