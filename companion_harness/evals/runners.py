"""CLI entry point for the eval subsystem.

Usage:
    python -m companion_harness.evals run --adapter harness_native --output reports/

Phase A ships only --adapter harness_native. Other adapter names exit 2.
--timing-mode synthetic_clock raises NotImplementedError (Phase A.5 deferral).
"""

from __future__ import annotations

import argparse
import sys


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
        # A3 wires the harness_native adapter; Phase A2 skeleton returns 0.
        print(f"[eval] adapter={args.adapter} output={args.output} split={args.split}")
        return 0

    parser.print_help()
    return 0
