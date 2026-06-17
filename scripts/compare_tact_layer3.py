"""Compare two models on the TACT-Bench Layer-3 mechanical scorer (SP4 deliverable).

    PYTHONPATH=. python scripts/compare_tact_layer3.py \
        --a-label MiniCPM-o --a <dir|decisions.json> \
        --b-label gpt-realtime-2 --b <dir-with-run*/  |  dir|decisions.json> \
        --output experiments/gpt-realtime/results/comparison

Re-scores each model's saved `decisions.json` with the shared `score_all` (no model
run, no network) and emits a side-by-side md+html: per-metric 4-arm table with each
model's value, run-to-run **spread** (max-min) when multiple runs are given, the
GPT-minus-MiniCPM delta, and a flag for deltas that exceed the GPT spread. An
optional `analysis.md` in the output dir is appended as the written analysis.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
from datetime import date
from pathlib import Path

import sys

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))   # repo root (companion_harness)
sys.path.insert(0, str(_HERE))          # sibling scripts (run_tact_layer3 helpers)

import run_tact_layer3 as _runner  # noqa: E402 — reuse _setup_md / descriptions / md->html
from companion_harness.evals.adapters.tact_bench import _ARMS as _ARM_PROMPTS  # noqa: E402
from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3, score_all  # noqa: E402

_ARMS = ["vanilla", "prompted", "monitor_stream", "audio"]
_METRICS = [
    ("action_accuracy", "higher"),
    ("form_accuracy", "higher"),
    ("cried_wolf", "lower"),
    ("urgent_miss", "lower"),
]


def _decisions_paths(path: Path) -> list[Path]:
    """A single decisions.json, or every run*/decisions.json under a results dir."""
    if path.is_file():
        return [path]
    runs = sorted(path.glob("run*/decisions.json"))
    if runs:
        return runs
    single = path / "decisions.json"
    if single.exists():
        return [single]
    raise FileNotFoundError(f"no decisions.json under {path}")


def _score_runs(path: Path, cases) -> dict:
    """{arm: {metric: [values across runs]}} for the arms present."""
    agg: dict[str, dict[str, list]] = {a: {m: [] for m, _ in _METRICS} for a in _ARMS}
    n_runs = 0
    for dp in _decisions_paths(path):
        decisions = json.loads(dp.read_text())
        n_runs += 1
        for arm in _ARMS:
            if arm in decisions:
                s = score_all(cases, decisions[arm])
                for m, _ in _METRICS:
                    v = s.get(m)
                    if isinstance(v, (int, float)):
                        agg[arm][m].append(v)
    return {"by_arm": agg, "n_runs": n_runs}


def _stat(vals: list) -> dict:
    if not vals:
        return {"mean": None, "min": None, "max": None, "spread": None, "n": 0}
    return {
        "mean": sum(vals) / len(vals), "min": min(vals), "max": max(vals),
        "spread": max(vals) - min(vals), "n": len(vals),
    }


def _fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "—"


def _cell(st: dict) -> str:
    if st["n"] == 0:
        return "—"
    if st["n"] == 1 or st["spread"] == 0:
        return _fmt(st["mean"])
    return f"{st['mean']:.2f} ±{st['spread']:.2f}"


def _delta_note(a: dict, b: dict) -> str:
    if a["n"] == 0 or b["n"] == 0:
        return "—"
    d = b["mean"] - a["mean"]
    noise = max(b["spread"] or 0.0, 0.0)
    flag = "**" if abs(d) > noise else ""   # exceeds GPT run-to-run spread
    return f"{flag}{d:+.2f}{flag}"


def _chart(a_label, b_label, a, b) -> str:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    fig, axes = plt.subplots(2, 2, figsize=(11, 7))
    x = np.arange(len(_ARMS))
    w = 0.38
    for ax, (m, direction) in zip(axes.flat, _METRICS):
        av = [(_stat(a["by_arm"][arm][m])["mean"] or 0.0) for arm in _ARMS]
        bv = [(_stat(b["by_arm"][arm][m])["mean"] or 0.0) for arm in _ARMS]
        berr = [(_stat(b["by_arm"][arm][m])["spread"] or 0.0) / 2 for arm in _ARMS]
        ax.bar(x - w / 2, av, w, label=a_label)
        ax.bar(x + w / 2, bv, w, yerr=berr, capsize=3, label=b_label)
        ax.set_title(f"{m} ({direction} better)", fontsize=9)
        ax.set_xticks(x)
        ax.set_xticklabels(_ARMS, rotation=15, ha="right", fontsize=7)
        ax.set_ylim(0, 1.05)
        ax.legend(fontsize=7)
    fig.suptitle("TACT-Bench Layer-3 — model comparison (mechanical, no judge)")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def _cases_md(cases, descriptions) -> str:
    L = ["## Test cases (plain language; gt = ground-truth action@tick)", ""]
    for c in cases:
        L.append(f"### {c.id} — {c.ticks} ticks")
        desc = _runner._describe(descriptions.get(c.id, {}))
        if desc:
            L += [f"> {desc}", ""]
        for it in c.items:
            exp = f"NOW:{it.expected.form}" if it.expected.kind == "NOW" else it.expected.kind
            L.append(f"- **{it.id}** (u={it.u} r={it.r} s={it.s}, t_avail={it.t_avail}) — "
                     f"gt: **{exp}@{it.decisive_tick}**")
        L.append("")
    return "\n".join(L)


def _md(a_label, b_label, a, b, analysis, cases, descriptions) -> str:
    L = ["# TACT-Bench Layer-3 — model comparison (mechanical scorer, no LLM judge)", "",
         f"- **{a_label}**: {a['n_runs']} run(s) · **{b_label}**: {b['n_runs']} run(s) · "
         f"28 cases / 35 items · {date.today().isoformat()}",
         "- Deterministic per-tick scoring. Cells show the metric mean; `±x` is the "
         f"run-to-run spread (max−min). Δ = {b_label} − {a_label}; **bold** Δ exceeds the "
         f"{b_label} spread (i.e. a real gap, not run noise).", "",
         _runner._setup_md(_ARMS), ""]
    for m, direction in _METRICS:
        L += [f"## {m} ({direction} better)", "",
              f"| arm | {a_label} | {b_label} | Δ |", "|---|---|---|---|"]
        for arm in _ARMS:
            sa, sb = _stat(a["by_arm"][arm][m]), _stat(b["by_arm"][arm][m])
            L.append(f"| {arm} | {_cell(sa)} | {_cell(sb)} | {_delta_note(sa, sb)} |")
        L.append("")
    if analysis:
        L += ["## Analysis", "", analysis, ""]
    L.append(_cases_md(cases, descriptions))
    return "\n".join(L)


def _inline(s: str) -> str:
    import html
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def _analysis_html(text: str) -> str:
    out = []
    for para in (text or "").split("\n\n"):
        para = para.strip()
        if not para:
            continue
        lines = para.splitlines()
        if all(ln.lstrip().startswith("- ") for ln in lines):
            out.append("<ul>" + "".join(f"<li>{_inline(ln.lstrip()[2:])}</li>" for ln in lines) + "</ul>")
        else:
            out.append(f"<p>{_inline(' '.join(lines))}</p>")
    return "\n".join(out)


def _setup_html() -> str:
    import html
    body = (
        "<h2>Setup &amp; the four arms</h2>"
        "<p>Identical 28-case Layer-3 bank and identical <b>Mode-A</b> per-tick probes — the "
        "harness owns the 1&nbsp;s clock and states <i>“second t + user state”</i> in every "
        "probe; deterministic mechanical scoring, no LLM judge. The arms differ only by system "
        "prompt; <code>audio</code> additionally feeds the probe as speech (audio in, text out).</p>")
    for arm in _ARMS:
        body += (f"<details><summary><b>{arm}</b> system prompt</summary>"
                 f"<pre class='prompt'>{html.escape(_ARM_PROMPTS.get(arm, '?'))}</pre></details>")
    return body


def _cases_html(cases, descriptions) -> str:
    blocks = []
    for c in cases:
        items = []
        for it in c.items:
            exp = f"NOW:{it.expected.form}" if it.expected.kind == "NOW" else it.expected.kind
            items.append(f"<li><b>{it.id}</b> (u={it.u} r={it.r} s={it.s}, t_avail={it.t_avail}) "
                         f"— gt <b>{exp}@{it.decisive_tick}</b></li>")
        desc = _runner._describe(descriptions.get(c.id, {}))
        dh = f"<p class='casedesc'>{_runner._inline_html(desc)}</p>" if desc else ""
        blocks.append(f"<div class='case'><h3>{c.id} — {c.ticks} ticks</h3>{dh}<ul>{''.join(items)}</ul></div>")
    return ("<h2>Test cases <span class='dir'>(plain language; gt = ground-truth action@tick)</span></h2>"
            + "".join(blocks))


def _html(a_label, b_label, a, b, analysis, cases, descriptions) -> str:
    chart = _chart(a_label, b_label, a, b)
    sections = []
    for m, direction in _METRICS:
        rows = ""
        for arm in _ARMS:
            sa, sb = _stat(a["by_arm"][arm][m]), _stat(b["by_arm"][arm][m])
            rows += (f"<tr><td class='a'>{arm}</td><td>{_cell(sa)}</td><td>{_cell(sb)}</td>"
                     f"<td>{_delta_note(sa, sb).replace('**','')}</td></tr>")
        sections.append(
            f"<h2>{m} <span class='dir'>({direction} better)</span></h2>"
            f"<table><thead><tr><th>arm</th><th>{a_label}</th><th>{b_label}</th><th>Δ</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>")
    analysis_html = f"<h2>Analysis</h2>{_analysis_html(analysis)}" if analysis else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>TACT-Bench comparison</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:920px;margin:2rem auto;color:#1a1a1a}}
 h1{{font-size:1.5rem}} h2{{font-size:1.1rem;border-bottom:1px solid #ddd;margin-top:1.4rem}}
 table{{border-collapse:collapse;width:100%;font-size:.9rem;margin:.3rem 0}}
 th,td{{border:1px solid #ddd;padding:.35rem .5rem;text-align:center}} th{{background:#f4f6f8}}
 td.a{{text-align:left;font-family:ui-monospace,monospace}} .dir{{color:#777;font-size:.82rem}}
 img{{max-width:100%;border:1px solid #eee}} code{{font-size:.85em}}
 details{{margin:.3rem 0}} summary{{cursor:pointer}}
 pre.prompt{{background:#f7f7f8;border:1px solid #eee;padding:.5rem .7rem;white-space:pre-wrap;font-size:.82rem;border-radius:4px}}
 .case{{margin:.6rem 0;padding:.4rem .8rem;border:1px solid #eee;border-radius:6px}} .case h3{{font-size:1rem;margin:.2rem 0}}
 .casedesc{{margin:.2rem 0 .4rem;color:#555;font-size:.88rem;background:#fafafa;border-left:3px solid #ddd;padding:.3rem .6rem}}
</style></head><body>
<h1>TACT-Bench Layer-3 — model comparison</h1>
<p class="dir">{a_label} ({a['n_runs']} run) vs {b_label} ({b['n_runs']} run) · 28 cases / 35 items ·
deterministic per-tick scoring, no LLM judge · {date.today().isoformat()}<br>
Cells = mean; <code>±x</code> = run-to-run spread (max−min). Δ = {b_label} − {a_label}.</p>
{_setup_html()}
<img src="data:image/png;base64,{chart}">
{''.join(sections)}
{analysis_html}
{_cases_html(cases, descriptions)}
<footer style="color:#888;font-size:.8rem;margin-top:2rem">scripts/compare_tact_layer3.py ·
scorer: companion_harness/evals/adapters/tact_bench_layer3.py</footer>
</body></html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--a", required=True, help="model A: a decisions.json or a results dir")
    p.add_argument("--b", required=True, help="model B: a decisions.json, a results dir, or a dir with run*/")
    p.add_argument("--a-label", default="model A")
    p.add_argument("--b-label", default="model B")
    p.add_argument("--output", required=True)
    args = p.parse_args()

    cases = load_layer3()
    a = _score_runs(Path(args.a), cases)
    b = _score_runs(Path(args.b), cases)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    analysis = (out / "analysis.md").read_text() if (out / "analysis.md").exists() else ""
    descriptions = _runner._load_descriptions()

    (out / "comparison.md").write_text(_md(args.a_label, args.b_label, a, b, analysis, cases, descriptions))
    (out / "comparison.html").write_text(_html(args.a_label, args.b_label, a, b, analysis, cases, descriptions))
    (out / "comparison_scores.json").write_text(json.dumps(
        {args.a_label: a, args.b_label: b}, indent=2))
    print(f"wrote {out}/comparison.md, comparison.html, comparison_scores.json")
    print(f"  {args.a_label}: {a['n_runs']} run(s); {args.b_label}: {b['n_runs']} run(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
