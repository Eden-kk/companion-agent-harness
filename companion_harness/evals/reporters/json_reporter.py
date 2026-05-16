"""JSON reporter — writes metrics.json and failure_slices.json.

eval-subsystem-spec.md §CLI surface output tree.
Task A4: plan-eval-phase-a-execution.md.
"""

from __future__ import annotations

import json
from pathlib import Path

from companion_harness.evals.schemas import BenchmarkResult, FailureSlice


class JsonReporter:
    """Writes ``metrics.json`` and ``failure_slices.json`` under ``<output_dir>/<run_id>/``.

    ``run.json`` (the ReplayRun envelope) is written by the runner (A3), not here.
    Both files use ``indent=2, sort_keys=True`` for diff-friendly output
    (Tier-B reproducibility hygiene).
    """

    def __init__(self, run_id: str, failure_slices: list[FailureSlice] | None = None) -> None:
        self._run_id = run_id
        self._failure_slices: list[FailureSlice] = failure_slices or []

    def render(self, results: list[BenchmarkResult], output_dir: Path) -> None:
        run_dir = output_dir / self._run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        metrics_payload: list[dict] = []
        for result in results:
            for mv in result.metrics:
                metrics_payload.append(
                    {
                        "case_id": result.case_id,
                        "name": mv.name,
                        "value": mv.value,
                        "unit": mv.unit,
                        "aggregation": mv.aggregation,
                    }
                )

        slices_payload = [
            {
                "case_id": fs.case_id,
                "causal_event_ids": list(fs.causal_event_ids),
                "suspected_adapter": fs.suspected_adapter,
            }
            for fs in self._failure_slices
        ]

        (run_dir / "metrics.json").write_text(
            json.dumps(metrics_payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        (run_dir / "failure_slices.json").write_text(
            json.dumps(slices_payload, indent=2, sort_keys=True), encoding="utf-8"
        )
