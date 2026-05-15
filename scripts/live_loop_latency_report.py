"""Live-loop offline latency report (v0.1d Task 7 main, v0.1e Task 6 migration).

Reads an events.json (or equivalent EventLogger-on-disk path), invokes
companion_harness.live_loop_metrics.compute_metrics(), and prints a
JSON-and-human report in the same format as scripts/v0_1a_replay_report.py.

Usage:
    python scripts/live_loop_latency_report.py --log-path PATH [--trace-dir DIR] [--json-only] [--show-trials]

--trace-dir: directory containing DecisionTrace JSON files (decision_traces/<id>.json).
             Defaults to decision_traces/ relative to --log-path's parent directory.

NOT_MEASURED legend:
  status_reason starting with "physical_audio_path" → PERMANENT (Task 8 dependency missing).
  All other NOT_MEASURED reasons → TRANSIENT (more samples or config fix may resolve).

Output format mirrors v0_1a_replay_report.py gate-status summary.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from companion_harness.live_loop_metrics import MetricResult, compute_metrics
from companion_harness.schemas import Event


def _load_events(log_path: str) -> list[Event]:
    with open(log_path) as f:
        raw = json.load(f)
    events: list[Event] = []
    for item in raw:
        events.append(Event(
            event_id=item["event_id"],
            session_id=item["session_id"],
            schema_version=item.get("schema_version", "0.1"),
            seq_no=item.get("seq_no", 0),
            event_type=item["event_type"],
            timestamp_mono_ms=item["timestamp_mono_ms"],
            timestamp_wall=item.get("timestamp_wall", ""),
            source=item.get("source", ""),
            caused_by=item.get("caused_by", []),
            payload_hash=item.get("payload_hash", ""),
            payload_ref=item.get("payload_ref"),
            payload_kind=item.get("payload_kind", "signal"),
            subject_class=item.get("subject_class", "unknown"),
            sensitivity=item.get("sensitivity", "safe"),
            retention_policy_id=item.get("retention_policy_id", "default"),
        ))
    return events


def _format_human(results: dict[str, MetricResult], show_trials: bool) -> str:
    lines: list[str] = ["=" * 68, "  Live-Loop Latency Report", "=" * 68, ""]
    lines.append("  Metrics:")
    lines.append("-" * 68)

    for name, r in results.items():
        status_str = r.status
        if r.status == "NOT_MEASURED":
            reason = r.status_reason
            is_permanent = reason.startswith("physical_audio_path")
            qualifier = " [PERMANENT — Task 8 dependency]" if is_permanent else " [TRANSIENT]"
            lines.append(f"  [{status_str:<12}]  {name}")
            lines.append(f"               reason:  {reason}{qualifier}")
        else:
            lines.append(f"  [{status_str:<12}]  {name}")
            if r.p50_ms is not None:
                gate_p50 = f"  (gate: {r.gate_threshold_p50_ms}ms)" if r.gate_threshold_p50_ms is not None else ""
                lines.append(f"               p50:     {r.p50_ms}ms{gate_p50}")
            if r.p95_ms is not None:
                gate_p95 = f"  (gate: {r.gate_threshold_p95_ms}ms)" if r.gate_threshold_p95_ms is not None else ""
                lines.append(f"               p95:     {r.p95_ms}ms{gate_p95}")
            lines.append(f"               n:       {r.sample_count}")
            if r.status_reason and r.status_reason not in ("ok", "observational"):
                lines.append(f"               note:    {r.status_reason}")

        if show_trials and r.trials:
            lines.append(f"               trials ({len(r.trials)}):")
            for t in r.trials:
                lines.append(f"                 {t.start_event_id} → {t.end_event_id}: {t.latency_ms}ms")
        lines.append("")

    lines += [
        "-" * 68,
        "  Legend:",
        "    MET      — gate threshold satisfied (n >= 3)",
        "    NOT_MET  — gate threshold exceeded (n >= 3)",
        "    NOT_MEASURED — insufficient samples or dependency missing",
        "    PERMANENT: status_reason starts with 'physical_audio_path' → Task 8 required",
        "    TRANSIENT: all other NOT_MEASURED reasons → re-run with more samples",
        "=" * 68,
    ]
    return "\n".join(lines)


def _build_report(results: dict[str, MetricResult], log_path: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "run_id": f"live-loop-latency-{uuid.uuid4().hex[:8]}",
        "report_type": "live_loop_latency",
        "log_path": log_path,
        "generated_at": now,
        "metrics": {
            name: {
                "metric_name": r.metric_name,
                "p50_ms": r.p50_ms,
                "p95_ms": r.p95_ms,
                "sample_count": r.sample_count,
                "status": r.status,
                "status_reason": r.status_reason,
                "gate_threshold_p50_ms": r.gate_threshold_p50_ms,
                "gate_threshold_p95_ms": r.gate_threshold_p95_ms,
                "trial_count": len(r.trials),
            }
            for name, r in results.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Emit live-loop latency report.")
    parser.add_argument("--log-path", required=True, help="Path to events.json log file.")
    parser.add_argument("--trace-dir", default=None, help="Path to decision_traces/ directory.")
    parser.add_argument("--json-only", action="store_true", help="Emit JSON only, no summary.")
    parser.add_argument("--show-trials", action="store_true", help="Print per-trial event pairs.")
    args = parser.parse_args()

    if args.trace_dir is not None:
        trace_dir = Path(args.trace_dir)
    else:
        trace_dir = Path(args.log_path).parent / "decision_traces"

    events = _load_events(args.log_path)
    results = compute_metrics(events, trace_dir=trace_dir if trace_dir.exists() else None)
    report = _build_report(results, args.log_path)

    if not args.json_only:
        print(_format_human(results, show_trials=args.show_trials))
        print()

    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
