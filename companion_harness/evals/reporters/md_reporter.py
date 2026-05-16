"""Markdown reporter — writes report.md.

eval-subsystem-spec.md §CLI surface output tree.
Task A4: plan-eval-phase-a-execution.md.
No rich/pandas imports — Phase A scope (Anchor 2 defers those to Phase B+).
"""

from __future__ import annotations

import textwrap
from datetime import datetime, timezone
from pathlib import Path

from companion_harness.evals.schemas import BenchmarkResult, FailureSlice


class MarkdownReporter:
    """Writes ``report.md`` under ``<output_dir>/<run_id>/``.

    Sections: header → summary → per-case → aggregate.
    Pure string formatting; no external libraries.
    """

    def __init__(
        self,
        run_id: str,
        benchmark_name: str = "",
        benchmark_version: str = "",
        run_timestamp: str = "",
        failure_slices: list[FailureSlice] | None = None,
    ) -> None:
        self._run_id = run_id
        self._benchmark_name = benchmark_name
        self._benchmark_version = benchmark_version
        self._run_timestamp = run_timestamp or datetime.now(timezone.utc).isoformat()
        self._failure_slices: list[FailureSlice] = failure_slices or []

    def render(self, results: list[BenchmarkResult], output_dir: Path) -> None:
        run_dir = output_dir / self._run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []

        # Header
        lines.append(f"# Eval run {self._run_id}")
        if self._benchmark_name:
            lines.append(f"\nBenchmark: **{self._benchmark_name}**"
                         + (f" v{self._benchmark_version}" if self._benchmark_version else ""))
        lines.append(f"\nTimestamp: {self._run_timestamp}")

        # Summary
        total = len(results)
        passed = sum(1 for r in results if r.pass_)
        lines.append("\n## Summary\n")
        if total == 0:
            lines.append("No cases run.")
        else:
            lines.append(f"- Total cases: {total}")
            lines.append(f"- Passed: {passed}")
            lines.append(f"- Failed: {total - passed}")
            lines.append(f"- Pass rate: {passed / total:.1%}")

        # Per-case
        if results:
            lines.append("\n## Cases\n")
        for result in results:
            status = "PASS" if result.pass_ else "FAIL"
            lines.append(f"### {result.case_id} — {status}\n")
            lines.append(f"Status: `{result.final_status}`\n")

            # Event log link
            lines.append(f"[event log](event_logs/{result.case_id}.jsonl)\n")

            # Metrics table
            if result.metrics:
                lines.append("| name | value | unit | aggregation |")
                lines.append("|---|---|---|---|")
                for mv in result.metrics:
                    val = json_safe_str(mv.value)
                    unit = mv.unit or ""
                    lines.append(f"| {mv.name} | {val} | {unit} | {mv.aggregation} |")
                lines.append("")

            # Failure slices for this case
            case_slices = [fs for fs in self._failure_slices if fs.case_id == result.case_id]
            for fs in case_slices:
                lines.append("#### Failure slice\n")
                shown = fs.causal_event_ids[:5]
                remainder = len(fs.causal_event_ids) - len(shown)
                for eid in shown:
                    lines.append(f"- {eid}")
                if remainder > 0:
                    lines.append(f"- ... and {remainder} more")
                if fs.suspected_adapter:
                    lines.append(f"\nSuspected adapter: `{fs.suspected_adapter}`")
                lines.append("")

        # Aggregate
        if results:
            lines.append("\n## Aggregate\n")
            lines.append(f"- Cases: {total}")
            lines.append(f"- Pass rate: {passed / total:.1%}")

        (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def json_safe_str(value: float | int | dict) -> str:
    if isinstance(value, dict):
        import json
        return json.dumps(value)
    return str(value)
