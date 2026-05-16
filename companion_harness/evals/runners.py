"""CLI entry point for the eval subsystem.

Usage:
    python -m companion_harness.evals run --adapter harness_native --output reports/
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path


class _RunConfig:
    """Minimal run configuration passed to scenario drivers."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir


def _name_prefix(name: str) -> str:
    return "".join(w[0] for w in name.replace("-", "_").split("_") if w)


def _run_adapter(info: object, output: str, split: str) -> int:
    from companion_harness.evals.registry import AdapterInfo
    assert isinstance(info, AdapterInfo)

    prefix = _name_prefix(info.name)
    run_id = f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    output_dir = Path(output) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    adapter = info.build()
    print(f"run_id={run_id}")

    case_results: list[dict] = []
    has_error = False

    if info.name == "harness_native":
        # harness_native uses a synchronous _run_sync path
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
    else:
        if not hasattr(adapter.scenario_driver, "run"):
            print(
                f"adapter '{info.name}' scenario_driver has no async run() method.",
                file=sys.stderr,
            )
            return 2

        async def _run_all() -> None:
            nonlocal has_error
            for case in adapter.case_source.iter_cases(split):
                replay_run = await adapter.scenario_driver.run(case, None, _RunConfig(output_dir))
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

        asyncio.run(_run_all())

    run_json_path = output_dir / "run.json"
    run_json_path.write_text(json.dumps({"run_id": run_id, "adapter": info.name, "cases": case_results}, indent=2))
    print(f"[eval] wrote {run_json_path}")

    return 1 if has_error else 0


# Kept as internal alias for one cycle (PR1 compat).
def _run_harness_native(output: str, split: str) -> int:
    from companion_harness.evals.registry import ADAPTERS
    return _run_adapter(ADAPTERS["harness_native"], output, split)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m companion_harness.evals",
        description="Companion-harness eval runner.",
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run a benchmark adapter.")
    run_p.add_argument(
        "--adapter",
        required=True,
        metavar="NAME",
        help="Adapter name. See ADAPTERS registry for available names.",
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
        from companion_harness.evals.registry import ADAPTERS
        info = ADAPTERS.get(args.adapter)
        if info is None:
            print(
                f"adapter '{args.adapter}' is not registered. "
                f"Known: {sorted(ADAPTERS)}. "
                "File new-adapter requests via `gh issue create --label eval-adapter`.",
                file=sys.stderr,
            )
            return 2
        if info.status == "disabled":
            print(f"adapter '{args.adapter}' is disabled: {info.notes or ''}", file=sys.stderr)
            return 2
        return _run_adapter(info, args.output, args.split)

    parser.print_help()
    return 0
