"""CLI entry point for the eval subsystem.

Usage:
    python -m companion_harness.evals run --adapter harness_native --output reports/
    python -m companion_harness.evals run --adapter candor --mode synthetic --limit 10
    python -m companion_harness.evals run --adapter full_duplex_bench_v1 --mode real --limit 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class _RunConfig:
    """Minimal run configuration passed to scenario drivers."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir


def _name_prefix(name: str) -> str:
    return "".join(w[0] for w in name.replace("-", "_").split("_") if w)


def _run_adapter(info: object, output: str, split: str, run_id: str | None = None) -> int:
    from companion_harness.evals.registry import AdapterInfo
    assert isinstance(info, AdapterInfo)

    if run_id is None:
        prefix = _name_prefix(info.name)
        run_id = f"{prefix}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    output_dir = Path(output) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
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

        # TODO(PR2): pass a threading.Event cancel flag when real-mode adapters land.
        # asyncio.run inside a thread cannot be cancelled by the outer task.cancel().
        asyncio.run(_run_all())

    finished_at = datetime.now(timezone.utc).isoformat()
    run_json_path = output_dir / "run.json"
    run_json_path.write_text(json.dumps({
        "run_id": run_id,
        "adapter": info.name,
        "started_at": started_at,
        "finished_at": finished_at,
        "cases": case_results,
    }, indent=2))
    print(f"[eval] wrote {run_json_path}")

    return 1 if has_error else 0


# Kept as internal alias for one cycle (PR1 compat).
def _run_harness_native(output: str, split: str) -> int:
    from companion_harness.evals.registry import ADAPTERS
    return _run_adapter(ADAPTERS["harness_native"], output, split)


def _run_candor(
    output: str,
    split: str,
    mode: str = "synthetic",
    limit: int | None = None,
) -> int:
    from companion_harness.evals.adapters.candor import CandorCaseSource, CandorScenarioDriver
    synthetic = mode == "synthetic"
    try:
        case_source = CandorCaseSource(synthetic=synthetic)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_id = f"candor-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    output_dir = Path(output) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    driver = CandorScenarioDriver()
    print(f"run_id={run_id}")

    case_results: list[dict] = []
    has_error = False

    async def _run_all() -> None:
        nonlocal has_error
        cases = case_source.iter_cases(split)
        count = 0
        for case in cases:
            if limit is not None and count >= limit:
                break
            replay_run = await driver.run(case, None, _RunConfig(output_dir))
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
            count += 1

    asyncio.run(_run_all())

    skip_stats = case_source.skip_stats()
    finished_at = datetime.now(timezone.utc).isoformat()
    run_json_path = output_dir / "run.json"
    run_json_path.write_text(json.dumps({
        "run_id": run_id,
        "adapter": "candor",
        "mode": mode,
        "limit": limit,
        "started_at": started_at,
        "finished_at": finished_at,
        "skip_stats": skip_stats,
        "skipped_row_count": skip_stats["skipped"],
        "total_row_count": skip_stats["attempted"],
        "cases": case_results,
    }, indent=2))
    print(f"[eval] wrote {run_json_path}")

    if skip_stats["attempted"] > 0 and skip_stats["skip_rate"] >= 0.05:
        print(
            f"[eval] ERROR: dataset_row_skip_rate={skip_stats['skip_rate']:.3f} >= 0.05 "
            f"({skip_stats['skipped']} skipped / {skip_stats['attempted']} attempted)",
            file=sys.stderr,
        )
        return 1

    return 1 if has_error else 0


def _run_fdb(
    output: str,
    split: str,
    mode: str = "synthetic",
    limit: int | None = None,
    version: str = "v1",
) -> int:
    from companion_harness.evals.adapters.full_duplex_bench import (
        FullDuplexBenchV1CaseSource,
        FullDuplexBenchV15CaseSource,
        _SyntheticFDBDriver,
    )
    synthetic = mode == "synthetic"
    try:
        if version == "v1":
            case_source: FullDuplexBenchV1CaseSource | FullDuplexBenchV15CaseSource = (
                FullDuplexBenchV1CaseSource(synthetic=synthetic)
            )
            adapter_name = "full_duplex_bench_v1"
        else:
            case_source = FullDuplexBenchV15CaseSource(synthetic=synthetic)
            adapter_name = "full_duplex_bench_v1_5"
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    run_id = f"fdb-{version}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    output_dir = Path(output) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    started_at = datetime.now(timezone.utc).isoformat()
    driver = _SyntheticFDBDriver()
    print(f"run_id={run_id}")

    case_results: list[dict] = []
    has_error = False

    async def _run_all() -> None:
        nonlocal has_error
        cases = case_source.iter_cases(split)
        count = 0
        for case in cases:
            if limit is not None and count >= limit:
                break
            replay_run = await driver.run(case, None, _RunConfig(output_dir))
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
            count += 1

    asyncio.run(_run_all())

    skip_stats = case_source.skip_stats()
    finished_at = datetime.now(timezone.utc).isoformat()
    run_json_path = output_dir / "run.json"
    run_json_path.write_text(json.dumps({
        "run_id": run_id,
        "adapter": adapter_name,
        "mode": mode,
        "limit": limit,
        "started_at": started_at,
        "finished_at": finished_at,
        "skip_stats": skip_stats,
        "skipped_row_count": skip_stats["skipped"],
        "total_row_count": skip_stats["attempted"],
        "cases": case_results,
    }, indent=2))
    print(f"[eval] wrote {run_json_path}")

    if skip_stats["attempted"] > 0 and skip_stats["skip_rate"] >= 0.05:
        print(
            f"[eval] ERROR: dataset_row_skip_rate={skip_stats['skip_rate']:.3f} >= 0.05 "
            f"({skip_stats['skipped']} skipped / {skip_stats['attempted']} attempted)",
            file=sys.stderr,
        )
        return 1

    return 1 if has_error else 0


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
        help="Adapter name. harness_native, candor, full_duplex_bench_v1, full_duplex_bench_v1_5, or registry name.",
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
    run_p.add_argument(
        "--mode",
        default="synthetic",
        choices=["synthetic", "real"],
        help="Adapter mode. synthetic (default) uses fixtures; real streams from HuggingFace.",
    )
    run_p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Cap the number of cases processed (per adapter). Omit for full corpus.",
    )

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "run":
        mode = getattr(args, "mode", "synthetic")
        limit = getattr(args, "limit", None)
        split = args.split
        output = args.output

        if args.adapter == "harness_native":
            return _run_harness_native(output, split)
        elif args.adapter == "candor":
            return _run_candor(output, split, mode, limit)
        elif args.adapter == "full_duplex_bench_v1":
            return _run_fdb(output, split, mode, limit, version="v1")
        elif args.adapter == "full_duplex_bench_v1_5":
            return _run_fdb(output, split, mode, limit, version="v1.5")
        else:
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
            return _run_adapter(info, output, split)

    parser.print_help()
    return 0
