"""Structural replay-safety contract for eval event logs.

eval-subsystem-spec.md Anchor 4 (Tier-B replay determinism).
architecture-v0.1.md invariants #1 and #5.

Phase A: structural check — asserts ReplayRun.event_log_path is set and
the JSONL file at that path is valid newline-delimited JSON.

The test then asserts Tier-B bit-identical replay, which requires
companion_harness/replay.py to expose a callable API. replay.py is
currently a docstring-only stub, so this assertion always fails.
The whole test is marked xfail(strict=False) so it is collected and
documents the gap without blocking the suite.

Deferred to Phase A.5. See plan-eval-phase-a-execution.md §F4.

Skips if pytest-json-report is not installed ([eval] extra not required in runtime CI).
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


@pytest.mark.xfail(
    reason="Tier-B bit-identical replay deferred to Phase A.5 — replay.py is a stub",
    strict=False,
)
@pytest.mark.skipif(not _has_json_report(), reason="pytest-json-report not installed ([eval] extra)")
def test_eval_run_replay_safe(tmp_path: Path) -> None:
    """Structural check + deferred Tier-B assertion (Anchor 4 + invariant #5)."""
    result = subprocess.run(
        [sys.executable, "-m", "companion_harness.evals", "run",
         "--adapter", "harness_native", "--output", str(tmp_path)],
        capture_output=True,
        text=True,
    )

    # Resolve run_id
    run_id: str | None = None
    for line in result.stdout.splitlines():
        m = re.match(r"run_id=(.+)", line)
        if m:
            run_id = m.group(1).strip()
            break
    if run_id is None:
        candidates = [d for d in tmp_path.iterdir() if d.is_dir() and d.name.startswith("hn-")]
        assert len(candidates) == 1
        run_id = candidates[0].name

    run_json_path = tmp_path / run_id / "run.json"
    assert run_json_path.exists(), f"run.json not found at {run_json_path}"

    run_data = json.loads(run_json_path.read_text(encoding="utf-8"))
    cases = run_data.get("cases", [])
    assert len(cases) == 4, f"Expected 4 case entries in run.json, got {len(cases)}"

    # Structural check: event_log_path is set and the JSONL file parses cleanly
    for case_entry in cases:
        event_log_path_str = case_entry.get("event_log_path")
        assert event_log_path_str, (
            f"ReplayRun.event_log_path not set for case {case_entry.get('case_id')!r}"
        )
        event_log_path = Path(event_log_path_str)
        assert event_log_path.exists(), (
            f"event_log_path {event_log_path} does not exist "
            f"for case {case_entry.get('case_id')!r}"
        )
        raw = event_log_path.read_text(encoding="utf-8").strip()
        assert raw, f"event log at {event_log_path} is empty"
        for i, line in enumerate(raw.splitlines()):
            try:
                json.loads(line)
            except json.JSONDecodeError as exc:
                raise AssertionError(
                    f"event log {event_log_path.name} line {i+1} is not valid JSON: {exc}"
                ) from exc

    # Tier-B bit-identical replay deferred to Phase A.5.
    # replay.py is a docstring-only stub; callable API not yet implemented.
    # See plan-eval-phase-a-execution.md §F4 and architecture-v0.1.md invariant #5.
    raise AssertionError(
        "replay.py callable API not yet implemented — "
        "Tier-B bit-identical replay deferred to Phase A.5; "
        "see plan-eval-phase-a-execution.md §F4"
    )
