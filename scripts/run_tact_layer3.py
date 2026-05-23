"""Run the TACT-Bench Layer-3 mechanical scorer across system-prompt arms (no judge).

    PYTHONPATH=. /raid/.../python scripts/run_tact_layer3.py \
        --arms vanilla prompted prompted_terse --output reports/tact-layer3

Loads MiniCPM-o once, drives the Mode-A NOW/WAIT/DROP elicitation per arm over all
28 Layer-3 cases, and grades each arm's decision set mechanically against the formal
gt — deterministic, no OpenAI. Reports arms alongside two reference baselines: the
gt oracle (ceiling) and never-deliver (floor). Emits Markdown + self-contained HTML.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion_harness.evals.adapters.tact_bench_layer3 import (  # noqa: E402
    load_layer3,
    perfect_decisions,
    score_all,
    score_item,
)
from companion_harness.evals.adapters.tact_bench_layer3_run import (  # noqa: E402
    load_context,
    run_arm,
)

_METRICS = [
    ("action_accuracy", "higher"),
    ("form_accuracy", "higher"),
    ("cried_wolf", "lower"),
    ("urgent_miss", "lower"),
]


def _shared_model_factory():
    holder: dict = {}

    def factory():
        if "m" not in holder:
            from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

            print("  loading MiniCPM-o (once)...", flush=True)
            t0 = time.monotonic()
            holder["m"] = MiniCPMStreamingModel()
            print(f"  loaded in {round((time.monotonic()-t0)*1000)}ms", flush=True)
        return holder["m"]

    return factory


def _fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


def _outcome_str(case, item_id, decisions) -> str:
    item = next(i for i in case.items if i.id == item_id)
    dec = (decisions.get(case.id, {}) or {}).get(item_id)
    r = score_item(case, item, dec)
    act = "—" if (not dec or not dec.get("action")) else f"{dec['action']}@{dec['tick']}"
    return f"{act} [{r.outcome}]"


def _chart(cols, scores) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    names = [n for n, _ in _METRICS]
    x = np.arange(len(names))
    w = 0.8 / max(len(cols), 1)
    fig, ax = plt.subplots(figsize=(9, 4.2))
    for i, c in enumerate(cols):
        vals = [scores[c].get(n) for n in names]
        ax.bar(x + i * w, [v if isinstance(v, (int, float)) else 0.0 for v in vals], w, label=c)
    ax.set_xticks(x + w * (len(cols) - 1) / 2)
    ax.set_xticklabels(names, rotation=15, ha="right", fontsize=8)
    ax.set_ylim(0, 1.05)
    ax.set_title("TACT-Bench Layer-3 (mechanical, no judge)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _md(cols, scores, cases, arms, decisions, meta) -> str:
    L = [f"# TACT-Bench Layer-3 — mechanical scorer (no LLM judge)", "",
         f"- Model: `{meta['model']}` | cases: {len(cases)} | arms: {', '.join(arms)} | date: {meta['date']}",
         "- Scoring is deterministic (per-tick gt); columns include the gt **oracle** (ceiling) "
         "and **never-deliver** (floor) for reference.", "",
         "## Metrics", "", "| metric | direction | " + " | ".join(cols) + " |",
         "|---|---|" + "---|" * len(cols)]
    for n, d in _METRICS:
        L.append(f"| {n} | {d} | " + " | ".join(_fmt(scores[c].get(n)) for c in cols) + " |")
    L += ["", "## Outcome counts (per arm)", "",
          "| arm | correct | wrong_form | miss | cried_wolf |", "|---|---|---|---|---|"]
    for a in arms:
        o = scores[a]["outcomes"]
        L.append(f"| {a} | {o.get('correct',0)} | {o.get('wrong_form',0)} | {o.get('miss',0)} | {o.get('cried_wolf',0)} |")
    L += ["", "## Per-case detail (gt vs each arm: action@tick [outcome])", ""]
    for c in cases:
        L.append(f"### {c.id} — {c.ticks} ticks")
        for it in c.items:
            exp = f"NOW:{it.expected.form}" if it.expected.kind == "NOW" else it.expected.kind
            L.append(f"- **{it.id}** (u={it.u} r={it.r} s={it.s}, t_avail={it.t_avail}) — "
                     f"gt: **{exp}@{it.decisive_tick}**")
            for a in arms:
                L.append(f"    - {a}: {_outcome_str(c, it.id, decisions[a])}")
        L.append("")
    return "\n".join(L)


def _html(cols, scores, cases, arms, decisions, meta) -> str:
    import html
    chart = _chart(cols, scores)
    head = "".join(f"<th>{c}</th>" for c in cols)
    mrows = "".join(
        f"<tr><td class='m'>{n}</td><td class='dir'>{d}</td>"
        + "".join(f"<td>{_fmt(scores[c].get(n))}</td>" for c in cols) + "</tr>"
        for n, d in _METRICS
    )
    blocks = []
    for c in cases:
        items_html = []
        for it in c.items:
            exp = f"NOW:{it.expected.form}" if it.expected.kind == "NOW" else it.expected.kind
            arms_li = "".join(
                f"<li>{a}: <code>{html.escape(_outcome_str(c, it.id, decisions[a]))}</code></li>"
                for a in arms
            )
            items_html.append(
                f"<p class='desc'><b>{it.id}</b> (u={it.u} r={it.r} s={it.s}, t_avail={it.t_avail}) — "
                f"gt <b>{exp}@{it.decisive_tick}</b></p><ul class='arms'>{arms_li}</ul>"
            )
        blocks.append(f"<div class='case'><h3>{c.id} — {c.ticks} ticks</h3>{''.join(items_html)}</div>")
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>TACT-Bench Layer-3</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:960px;margin:2rem auto;color:#1a1a1a}}
 h1{{font-size:1.5rem}} h2{{font-size:1.15rem;border-bottom:1px solid #ddd;margin-top:1.6rem}}
 table{{border-collapse:collapse;width:100%;font-size:.9rem}} th,td{{border:1px solid #ddd;padding:.35rem .5rem;text-align:center}}
 th{{background:#f4f6f8}} td.m{{text-align:left;font-family:ui-monospace,monospace}} td.dir{{color:#777;font-size:.82rem}}
 .meta{{color:#555;font-size:.9rem}} img{{max-width:100%;border:1px solid #eee}}
 .case{{margin:.7rem 0;padding:.4rem .8rem;border:1px solid #eee;border-radius:6px}} .case h3{{font-size:1rem;margin:.2rem 0}}
 .desc{{margin:.3rem 0 .1rem;color:#333}} .arms{{margin:.1rem 0 .4rem 1rem}} code{{font-size:.85em}}
</style></head><body>
<h1>TACT-Bench Layer-3 — mechanical scorer (no LLM judge)</h1>
<p class="meta">Model <code>{meta['model']}</code> · {len(cases)} cases · arms: {', '.join(arms)} · {meta['date']}<br>
Deterministic per-tick scoring; <b>oracle</b> = gt ceiling, <b>never</b> = silence floor.</p>
<h2>Metrics</h2>
<table><thead><tr><th>metric</th><th>direction</th>{head}</tr></thead><tbody>{mrows}</tbody></table>
<img src="data:image/png;base64,{chart}">
<h2>Per-case detail <span class="meta">(gt vs each arm: action@tick [outcome])</span></h2>
{''.join(blocks)}
<footer style="color:#888;font-size:.8rem;margin-top:2rem">Generated by scripts/run_tact_layer3.py ·
scorer: companion_harness/evals/adapters/tact_bench_layer3.py</footer>
</body></html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arms", nargs="+", default=["vanilla", "prompted", "monitor_stream"])
    p.add_argument("--output", default="reports/tact-layer3")
    args = p.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    cases = load_layer3()
    ctx = load_context()
    model_factory = _shared_model_factory()

    decisions: dict[str, dict] = {}
    scores: dict[str, dict] = {}
    for arm in args.arms:
        print(f"\n=== arm: {arm} ===", flush=True)
        decisions[arm] = run_arm(cases, model_factory(), arm, ctx)
        scores[arm] = score_all(cases, decisions[arm])
        print(f"  {scores[arm]}", flush=True)

    # reference baselines
    decisions["oracle"] = perfect_decisions(cases)
    scores["oracle"] = score_all(cases, decisions["oracle"])
    decisions["never"] = {c.id: {} for c in cases}
    scores["never"] = score_all(cases, decisions["never"])

    cols = list(args.arms) + ["oracle", "never"]
    meta = {"model": "openbmb/MiniCPM-o-4_5", "date": date.today().isoformat()}
    (out / "report.md").write_text(_md(cols, scores, cases, args.arms, decisions, meta))
    (out / "report.html").write_text(_html(cols, scores, cases, args.arms, decisions, meta))
    (out / "scores.json").write_text(json.dumps({c: scores[c] for c in cols}, indent=2))
    (out / "decisions.json").write_text(json.dumps(decisions, indent=2))
    print(f"\nWrote {out}/report.md, report.html, scores.json, decisions.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
