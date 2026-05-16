"""CLI entry point for the eval subsystem.

Usage:
    python -m companion_harness.evals run --adapter harness_native --output reports/

Phase A ships only --adapter harness_native. Other adapter names exit 2.
--timing-mode synthetic_clock raises NotImplementedError (Phase A.5 deferral).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path


class _RunConfig:
    """Minimal run configuration passed to scenario drivers."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir


def _run_harness_native(output: str, split: str) -> int:
    from companion_harness.evals.adapters import harness_native  # eval-internal import

    run_id = f"hn-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    output_dir = Path(output) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # Attempt to detect repo root (parent of companion_harness package)
    import companion_harness
    repo_root = Path(companion_harness.__file__).parent.parent

    adapter = harness_native.build(repo_root=repo_root)
    run_config = _RunConfig(output_dir=output_dir)

    print(f"run_id={run_id}")

    case_results: list[dict] = []
    has_error = False

    for case in adapter.case_source.iter_cases(split):
        replay_run = adapter.scenario_driver._run_sync(case, output_dir)  # type: ignore[attr-defined]
        passed = replay_run.final_status in ("completed", "skipped")
        if replay_run.final_status == "error":
            has_error = True
        case_results.append({
            "case_id": replay_run.case_id,
            "final_status": replay_run.final_status,
            "results": replay_run.results,
            "event_log_path": str(replay_run.event_log_path),
        })
        status_str = "OK" if passed else "ERROR"
        print(f"  [{status_str}] {case.case_id}: {replay_run.final_status}")

    run_json_path = output_dir / "run.json"
    run_json_path.write_text(json.dumps({"run_id": run_id, "cases": case_results}, indent=2))
    print(f"[eval] wrote {run_json_path}")

    return 1 if has_error else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m companion_harness.evals",
        description="Companion-harness eval runner (Phase A).",
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run a benchmark adapter.")
    run_p.add_argument(
        "--adapter",
        required=True,
        metavar="NAME",
        help="Adapter name (harness_native in Phase A).",
    )
    run_p.add_argument(
        "--timing-mode",
        default="wall_clock",
        choices=["wall_clock", "synthetic_clock"],
        dest="timing_mode",
        help="Timing mode (synthetic_clock available in Phase A.5).",
    )
    run_p.add_argument(
        "--output",
        default="reports/",
        metavar="DIR",
        help="Output directory for run artifacts.",
    )
    run_p.add_argument(
        "--split",
        default="test",
        metavar="SPLIT",
        help="Dataset split passed to CaseSource.iter_cases().",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "run":
        if args.timing_mode == "synthetic_clock":
            raise NotImplementedError(
                "synthetic_clock available in Phase A.5; "
                "see plan-eval-phase-a-execution.md §F5."
            )
        if args.adapter != "harness_native":
            print(
                f"adapter '{args.adapter}' not implemented in Phase A; "
                "only 'harness_native' is supported.",
                file=sys.stderr,
            )
            return 2
        return _run_harness_native(args.output, args.split)

    parser.print_help()
    return 0
