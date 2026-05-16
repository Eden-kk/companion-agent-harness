"""harness_native adapter — wraps 4 existing contract tests as EvaluationCases.

eval-subsystem-spec.md Anchor 5 (BenchmarkAdapter shape).
plan-eval-phase-a-execution.md Task A3.

Subprocess pytest is used per-case (not in-process pytest.main()) to preserve
Tier-B replay isolation (invariant #5 + plan A3 step 6 lean).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from companion_harness.evals.protocols import (
    BenchmarkAdapter,
    CaseSource,
    FailureSliceExtractor,
)
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice
from companion_harness.schemas import EvaluationCase, ReplayRun

# ---------------------------------------------------------------------------
# Static case list — 4 existing contract tests as EvaluationCases
# ---------------------------------------------------------------------------

_HARNESS_NATIVE_CASES: list[EvaluationCase] = [
    EvaluationCase(
        case_id="thinking_pause",
        stage=3,
        scenario="thinking_pause_during_question",
        modalities=["audio"],
        fixture_ref="thinking_pause_001/case.json",
        expected_events=["policy_decision"],
        expected_metrics={"thinking_pause_false_positive_rate": "= 0"},
        consent_class="safe_eval_fixture",
        benchmark_name="harness_native",
        benchmark_version="v1",
        inputs={"pytest_node": "tests/test_thinking_pause.py::test_thinking_pause"},
        expected_behavior={"pytest_status": "passed"},
        fixtures=["companion_harness/fixtures/thinking_pause_001/"],
    ),
    EvaluationCase(
        case_id="explicit_turn_handoff",
        stage=1,
        scenario="explicit_turn_handoff",
        modalities=["audio"],
        fixture_ref="explicit_turn_handoff_001/case.json",
        expected_events=["policy_decision"],
        expected_metrics={},
        consent_class="safe_eval_fixture",
        benchmark_name="harness_native",
        benchmark_version="v1",
        inputs={"pytest_node": "tests/test_explicit_turn_handoff.py::test_explicit_turn_handoff"},
        expected_behavior={"pytest_status": "passed"},
        fixtures=["companion_harness/fixtures/explicit_turn_handoff_001/"],
    ),
    EvaluationCase(
        case_id="barge_in",
        stage=1,
        scenario="barge_in_stop_latency",
        modalities=["audio"],
        fixture_ref="barge_in_001/case.json",
        expected_events=["assistant_audio_stop_completed"],
        expected_metrics={"assistant_stop_latency_ms_p95": "<200"},
        consent_class="safe_eval_fixture",
        benchmark_name="harness_native",
        benchmark_version="v1",
        inputs={"pytest_node": "tests/test_barge_in.py::test_barge_in"},
        expected_behavior={"pytest_status": "passed"},
        fixtures=["companion_harness/fixtures/barge_in_001/"],
    ),
    EvaluationCase(
        case_id="false_interruption_rate",
        stage=1,
        scenario="false_interruption_rate_10min",
        modalities=["audio"],
        fixture_ref="false_interruption_001/case.json",
        expected_events=["policy_decision"],
        expected_metrics={"false_interruption_count_per_10_min": "<1"},
        consent_class="safe_eval_fixture",
        benchmark_name="harness_native",
        benchmark_version="v1",
        inputs={"pytest_node": "tests/test_false_interruption_rate.py::test_false_interruption_rate"},
        expected_behavior={"pytest_status": "passed"},
        fixtures=["companion_harness/fixtures/false_interruption_001/"],
    ),
]


# ---------------------------------------------------------------------------
# Private implementations — live alongside the adapter that needs them
# ---------------------------------------------------------------------------

@dataclass
class _StaticCaseSource:
    """Yields a fixed list of EvaluationCases; split argument is ignored."""

    _cases: list[EvaluationCase]
    name: str = "harness_native"
    version: str = "v1"

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]:
        return iter(self._cases)


assert isinstance(_StaticCaseSource(_HARNESS_NATIVE_CASES), CaseSource)


class _NoOpFailureSlicer:
    """Returns empty slices — concrete failure slicing is Phase B2."""

    def extract(
        self,
        case: EvaluationCase,
        replay_run: ReplayRun,
        result: BenchmarkResult,
    ) -> list[FailureSlice]:
        return []


assert isinstance(_NoOpFailureSlicer(), FailureSliceExtractor)


# ---------------------------------------------------------------------------
# Subprocess pytest driver
# ---------------------------------------------------------------------------

class HarnessNativePytestDriver:
    """Runs one EvaluationCase by shelling out to pytest.

    Subprocess isolation preserves Tier-B replay determinism (plan A3 OQ-A3.1).
    Uses sys.executable so the canonical venv on b200 is honoured automatically.
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self._repo_root = repo_root

    async def run(self, case: EvaluationCase, harness_factory: object, run_config: object) -> ReplayRun:
        """Protocol-required async entry point; delegates to synchronous subprocess call."""
        output_dir: Path = getattr(run_config, "output_dir", Path("reports"))
        return self._run_sync(case, output_dir)

    def _run_sync(self, case: EvaluationCase, output_dir: Path) -> ReplayRun:
        """Run one case synchronously; returns a ReplayRun with final_status set."""
        import subprocess  # stdlib — intentional local import keeps top-level clean

        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        started_mono = int(time.monotonic() * 1000)

        event_log_dir = output_dir / "event_logs"
        event_log_dir.mkdir(parents=True, exist_ok=True)
        event_log_path = event_log_dir / f"{case.case_id}.jsonl"

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as jf:
            json_report_file = jf.name

        pytest_node = case.inputs["pytest_node"] if case.inputs else ""

        cmd = [
            sys.executable,
            "-m",
            "pytest",
            pytest_node,
            "-v",
            "--json-report",
            f"--json-report-file={json_report_file}",
        ]

        cwd = str(self._repo_root) if self._repo_root else None
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)  # noqa: S603

        completed_mono = int(time.monotonic() * 1000)
        finished_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # Exit-code → final_status mapping (plan A3 step 6)
        if proc.returncode == 0:
            final_status = "completed"
        elif proc.returncode == 5:
            final_status = "skipped"
        else:
            final_status = "error"

        # Write enveloping events to per-case JSONL (invariant #1 — no unlogged behaviour)
        run_id = output_dir.name
        started_evt = {
            "event_id": f"bcs-{case.case_id}-start",
            "event_type": "benchmark_case_started",
            "case_id": case.case_id,
            "run_id": run_id,
            "caused_by": [f"benchmark_run_{run_id}"],
            "timestamp_wall": started_at,
        }
        completed_evt = {
            "event_id": f"bcs-{case.case_id}-end",
            "event_type": "benchmark_case_completed",
            "case_id": case.case_id,
            "run_id": run_id,
            "final_status": final_status,
            "caused_by": [f"bcs-{case.case_id}-start"],
            "timestamp_wall": finished_at,
        }

        with event_log_path.open("w") as fh:
            fh.write(json.dumps(started_evt) + "\n")
            fh.write(json.dumps(completed_evt) + "\n")

        # Clean up temp json report
        try:
            os.unlink(json_report_file)
        except OSError:
            pass

        return ReplayRun(
            run_id=run_id,
            case_id=case.case_id,
            implementation_config_version="harness_native-v1",
            policy_version="harness_native-v1",
            started_at=started_at,
            finished_at=finished_at,
            results={"pytest_exit_code": proc.returncode},
            failures=[],
            event_log_path=event_log_path,
            timing_mode="wall_clock",
            started_at_mono_ms=started_mono,
            completed_at_mono_ms=completed_mono,
            final_status=final_status,
        )


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------

def build(repo_root: Path | None = None) -> BenchmarkAdapter:
    """Return the harness_native BenchmarkAdapter instance."""
    return BenchmarkAdapter(
        name="harness_native",
        version="v1",
        case_source=_StaticCaseSource(_HARNESS_NATIVE_CASES),
        scenario_driver=HarnessNativePytestDriver(repo_root=repo_root),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpFailureSlicer(),
        reporters=[],
    )
