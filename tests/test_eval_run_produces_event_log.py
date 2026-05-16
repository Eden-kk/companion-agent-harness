"""Contract test: eval run produces per-case JSONL event logs.

Invokes python -m companion_harness.evals run --adapter harness_native and
asserts the event_logs/ directory contains 4 .jsonl files, each with
valid newline-delimited JSON where the first event is benchmark_case_started
and the last is benchmark_case_completed (invariant #1 — no unlogged behaviour).

Skips if pytest-json-report is not installed (eval extra not required in runtime CI).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest


def _has_json_report() -> bool:
    try:
        import pytest_jsonreport  # noqa: F401
        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_json_report(), reason="pytest-json-report not installed ([eval] extra)")
def test_eval_run_produces_event_log(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "companion_harness.evals", "run",
         "--adapter", "harness_native", "--output", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    # Parse run_id from stdout (runners.py prints "run_id=<value>")
    run_id: str | None = None
    for line in result.stdout.splitlines():
        m = re.match(r"run_id=(.+)", line)
        if m:
            run_id = m.group(1).strip()
            break

    if run_id is None:
        # Fallback: discover single hn-* subdirectory
        candidates = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("hn-")]
        assert len(candidates) == 1, (
            f"Expected exactly 1 hn-* run directory, found {[d.name for d in candidates]}; "
            f"runner stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        run_id = candidates[0].name

    event_log_dir = tmp_path / run_id / "event_logs"
    assert event_log_dir.is_dir(), (
        f"event_logs/ directory not found at {event_log_dir}; "
        f"runner stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )

    jsonl_files = sorted(event_log_dir.glob("*.jsonl"))
    assert len(jsonl_files) == 4, (
        f"Expected 4 .jsonl event log files, found {len(jsonl_files)}: "
        f"{[f.name for f in jsonl_files]}"
    )

    for jsonl_path in jsonl_files:
        case_id = jsonl_path.stem
        raw = jsonl_path.read_text(encoding="utf-8").strip()
        assert raw, f"event log for {case_id!r} is empty"

        lines = raw.splitlines()
        events: list[dict] = []
        for i, line in enumerate(lines):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"event log {jsonl_path.name} line {i+1} is not valid JSON: {exc}"
                ) from exc
            assert isinstance(obj, dict), (
                f"event log {jsonl_path.name} line {i+1} is not a JSON object"
            )
            for field in ("event_id", "event_type", "caused_by"):
                assert field in obj, (
                    f"invariant #1 — no unlogged behaviour — "
                    f"{jsonl_path.name} line {i+1} missing field {field!r}"
                )
            events.append(obj)

        assert events[0]["event_type"] == "benchmark_case_started", (
            f"invariant #1 — no unlogged behaviour — case {case_id!r} emitted no "
            f"benchmark_case_started event (got {events[0]['event_type']!r})"
        )
        assert events[-1]["event_type"] == "benchmark_case_completed", (
            f"invariant #1 — no unlogged behaviour — case {case_id!r} last event is not "
            f"benchmark_case_completed (got {events[-1]['event_type']!r})"
        )
