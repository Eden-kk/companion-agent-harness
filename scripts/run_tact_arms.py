"""Run the TACT-Bench adapter across multiple system-prompt arms (text-only) and
emit a side-by-side comparison report in Markdown + self-contained HTML.

Run on B200 (judge needs OPENAI_API_KEY for filled metrics):

    OPENAI_API_KEY=... /raid/.../python scripts/run_tact_arms.py \
        --arms vanilla prompted prompted_terse --output reports/tact-3arm

Loads MiniCPM-o ONCE and reuses it across arms. Per arm: drives all cases through
the adapter's TactMiniCPMDriver (input_mode=text, silence-clock), judges deliveries
with the OpenAI DeliveryJudge, aggregates the 5 metrics. The HTML report embeds a
matplotlib bar chart as base64 (no external assets).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

# Ensure THIS checkout's companion_harness wins over any installed copy (matters
# in a git worktree, where `python scripts/x.py` would otherwise import the
# editable-installed main checkout that lacks this adapter).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion_harness.evals.adapters.tact_bench import (  # noqa: E402
    TactCaseSource,
    aggregate_tact_metrics,
    build_tact_bench,
)


def _spoken_text(trajectory: list[dict]) -> str:
    parts = [c["text"].strip() for c in trajectory if not c["is_listen"] and c["text"].strip()]
    return " ".join(parts).strip()

_METRICS = [
    ("cried_wolf", "lower"),
    ("urgent_miss", "lower"),
    ("breakpoint_hit", "higher"),
    ("delivery_rate", "higher"),
    ("conditional_form", "higher"),
]


class _RC:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir


def _shared_model_factory():
    holder: dict = {}

    def factory():
        if "m" not in holder:
            from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

            print("  loading MiniCPM-o (once, shared across arms)...", flush=True)
            t0 = time.monotonic()
            holder["m"] = MiniCPMStreamingModel()
            print(f"  model loaded in {round((time.monotonic() - t0) * 1000)}ms", flush=True)
        return holder["m"]

    return factory


async def _run_arm(arm: str, model_factory, input_mode: str, out_dir: Path) -> dict:
    adapter = build_tact_bench(arm=arm, input_mode=input_mode, model_factory=model_factory)
    runs = []
    per_case = {}
    for case in adapter.case_source.iter_cases("all"):
        rr = await adapter.scenario_driver.run(case, None, _RC(out_dir / arm))
        label = None
        if adapter.examiner is not None and (rr.results or {}).get("trajectory") is not None:
            label = adapter.examiner.label(
                rr.results["trajectory"], rr.results.get("item") or {},
                (case.inputs or {}).get("user_script"),
            )
            rr.results["judge_label"] = label
        runs.append(rr)
        per_case[case.case_id] = {
            "label": label,
            "spoke": _spoken_text(rr.results["trajectory"]) or "(silent)",
        }
        print(f"    [{arm}] {case.case_id}: "
              f"delivered={None if not label else label['delivered']} "
              f"form={None if not label else label['form']}", flush=True)
    return {"metrics": aggregate_tact_metrics(runs), "per_case": per_case}


def _fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


def _md_report(arms, results, cases, meta) -> str:
    by_id = {c.case_id: c for c in cases}
    L = []
    L.append("# TACT-Bench — multi-arm comparison (text-only)")
    L.append("")
    L.append(f"- Model: `{meta['model']}` | input mode: {meta['input_mode']} | "
             f"judge: {meta['judge']} | cases: {meta['n_cases']} | date: {meta['date']}")
    L.append(f"- Arms: {', '.join(arms)}")
    L.append("")
    L.append("## Metric comparison")
    L.append("")
    L.append("| Metric | direction | " + " | ".join(arms) + " |")
    L.append("|---|---|" + "---|" * len(arms))
    for name, direction in _METRICS:
        row = " | ".join(_fmt(results[a]["metrics"].get(name)) for a in arms)
        L.append(f"| {name} | {direction} | {row} |")
    L.append("")
    L.append("## Per-case delivery (delivered@chunk / form)")
    L.append("")
    L.append("| case | behavior | expected_form | " + " | ".join(arms) + " |")
    L.append("|---|---|---|" + "---|" * len(arms))
    for cid in by_id:
        eb = by_id[cid].expected_behavior or {}
        cells = []
        for a in arms:
            lb = results[a]["per_case"].get(cid, {}).get("label")
            if not lb or lb.get("delivered") is None:
                cells.append("—")
            elif lb["delivered"]:
                cells.append(f"{lb['first_delivery_chunk']}/{lb['form']}")
            else:
                cells.append("no")
        L.append(f"| {cid} | {eb.get('behavior')} | {eb.get('expected_form') or '-'} | "
                 + " | ".join(cells) + " |")
    L.append("")
    L.append("## Verdict")
    L.append("")
    L.append("_TODO: interpret the arm differences._")
    L.append("")
    return "\n".join(L)


def _chart_base64(arms, results) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    names = [n for n, _ in _METRICS]
    x = np.arange(len(names))
    width = 0.8 / max(len(arms), 1)
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for i, a in enumerate(arms):
        vals = [results[a]["metrics"].get(n) for n in names]
        heights = [v if isinstance(v, (int, float)) else 0.0 for v in vals]
        bars = ax.bar(x + i * width, heights, width, label=a)
        for b, v in zip(bars, vals):
            if not isinstance(v, (int, float)):
                ax.text(b.get_x() + b.get_width() / 2, 0.02, "n/a", ha="center", va="bottom", fontsize=7, rotation=90)
    ax.set_xticks(x + width * (len(arms) - 1) / 2)
    ax.set_xticklabels(names, rotation=20, ha="right", fontsize=8)
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("rate")
    ax.set_title("TACT-Bench metrics by arm (text-only)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _html_report(arms, results, cases, meta) -> str:
    by_id = {c.case_id: c for c in cases}
    chart = _chart_base64(arms, results)

    def metric_rows() -> str:
        out = []
        for name, direction in _METRICS:
            tds = "".join(f"<td>{_fmt(results[a]['metrics'].get(name))}</td>" for a in arms)
            out.append(f"<tr><td class='m'>{name}</td><td class='dir'>{direction}</td>{tds}</tr>")
        return "\n".join(out)

    def case_rows() -> str:
        out = []
        for cid in by_id:
            eb = by_id[cid].expected_behavior or {}
            tds = []
            for a in arms:
                lb = results[a]["per_case"].get(cid, {}).get("label")
                if not lb or lb.get("delivered") is None:
                    cell, cls = "—", "na"
                elif lb["delivered"]:
                    cell, cls = f"{lb['first_delivery_chunk']}/{lb['form']}", "yes"
                else:
                    cell, cls = "no", "no"
                tds.append(f"<td class='{cls}'>{cell}</td>")
            out.append(f"<tr><td>{cid}</td><td>{eb.get('behavior')}</td>"
                       f"<td>{eb.get('expected_form') or '-'}</td>{''.join(tds)}</tr>")
        return "\n".join(out)

    arm_th = "".join(f"<th>{a}</th>" for a in arms)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>TACT-Bench multi-arm report</title>
<style>
  body {{ font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; margin: 2rem auto; max-width: 960px; color: #1a1a1a; }}
  h1 {{ font-size: 1.5rem; }} h2 {{ font-size: 1.15rem; margin-top: 1.8rem; border-bottom: 1px solid #ddd; padding-bottom: .2rem; }}
  .meta {{ color: #555; font-size: .9rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: .6rem 0; font-size: .9rem; }}
  th, td {{ border: 1px solid #ddd; padding: .35rem .5rem; text-align: center; }}
  th {{ background: #f4f6f8; }}
  td.m {{ text-align: left; font-family: ui-monospace, monospace; }}
  td.dir {{ color: #777; font-size: .82rem; }}
  td.yes {{ background: #e7f5e7; }} td.no {{ background: #fdeaea; }} td.na {{ color: #aaa; }}
  img {{ max-width: 100%; border: 1px solid #eee; }}
  footer {{ color: #888; font-size: .8rem; margin-top: 2rem; }}
</style></head><body>
<h1>TACT-Bench — multi-arm comparison (text-only)</h1>
<p class="meta">Model <code>{meta['model']}</code> · input mode <b>{meta['input_mode']}</b> ·
 judge {meta['judge']} · {meta['n_cases']} cases · {meta['date']}<br>
 Arms: {', '.join(arms)}</p>

<h2>Metric comparison</h2>
<table><thead><tr><th>metric</th><th>direction</th>{arm_th}</tr></thead>
<tbody>{metric_rows()}</tbody></table>
<img alt="metrics by arm" src="data:image/png;base64,{chart}">

<h2>Per-case delivery <span class="meta">(delivered@chunk / form; green=delivered, red=stayed silent, —=judge n/a)</span></h2>
<table><thead><tr><th>case</th><th>behavior</th><th>expected_form</th>{arm_th}</tr></thead>
<tbody>{case_rows()}</tbody></table>

<footer>Generated by scripts/run_tact_arms.py · metrics defined in
companion_harness/evals/adapters/tact_bench.py (plan-tact-bench-minicpm-adapter.md).</footer>
</body></html>
"""


async def _main(args) -> int:
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = list(TactCaseSource(input_mode=args.input_mode).iter_cases("all"))
    model_factory = _shared_model_factory()

    results = {}
    for arm in args.arms:
        print(f"\n=== arm: {arm} ===", flush=True)
        results[arm] = await _run_arm(arm, model_factory, args.input_mode, out_dir)
        print(f"  metrics: {results[arm]['metrics']}", flush=True)

    meta = {
        "model": "openbmb/MiniCPM-o-4_5",
        "input_mode": args.input_mode,
        "judge": ("OpenAI gpt-4o" if os.environ.get("OPENAI_API_KEY") else "NONE (PENDING)"),
        "n_cases": len(cases),
        "date": date.today().isoformat(),
    }
    (out_dir / "report.md").write_text(_md_report(args.arms, results, cases, meta))
    (out_dir / "report.html").write_text(_html_report(args.arms, results, cases, meta))
    (out_dir / "metrics.json").write_text(json.dumps(
        {a: results[a]["metrics"] for a in args.arms}, indent=2))
    print(f"\nWrote {out_dir}/report.md, report.html, metrics.json")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arms", nargs="+", default=["vanilla", "prompted", "prompted_terse"])
    p.add_argument("--input-mode", default="text", choices=["text", "audio"], dest="input_mode")
    p.add_argument("--output", default="reports/tact-3arm")
    return asyncio.run(_main(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
