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
import re
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion_harness.evals.adapters.tact_bench import _ARMS  # noqa: E402
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


_MODEL_LABELS = {
    "minicpm": "openbmb/MiniCPM-o-4_5",
    "gpt-realtime-2": "openai/gpt-realtime-2",
}


def _shared_model_factory(model_name: str = "minicpm"):
    holder: dict = {}

    def factory():
        if "m" not in holder:
            if model_name == "minicpm":
                from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

                print("  loading MiniCPM-o (once)...", flush=True)
                t0 = time.monotonic()
                holder["m"] = MiniCPMStreamingModel()
                print(f"  loaded in {round((time.monotonic()-t0)*1000)}ms", flush=True)
            else:
                from companion_harness.foreground_model_gpt_realtime import GptRealtimeModel

                print(f"  using OpenAI {model_name} via Chat Completions", flush=True)
                holder["m"] = GptRealtimeModel(model=model_name)
        return holder["m"]

    return factory


def _read_meta(out: Path, model_flag: str = "minicpm") -> dict:
    """Model/date for the report. Prefer the persisted meta.json (written by the live
    run) so re-renders aren't mislabelled; else derive from the --model flag."""
    meta_path = out / "meta.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    return {"model": _MODEL_LABELS.get(model_flag, model_flag), "date": date.today().isoformat()}


def _fmt(v) -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else "n/a"


_BACKGROUND = (
    "**What this measures.** TACT-Bench scores *when an always-on assistant should surface "
    "a result it already holds* on its single audio channel — deliver now / defer to a pause "
    "/ drop, and in what form — with **staying silent a first-class correct action**. Layer 3 "
    "is the formal per-tick ground truth, scored **mechanically (no LLM judge)**, so results "
    "are deterministic and reproducible.\n\n"
    "**Cases.** 28 model-agnostic scenarios (TC1–TC29, no TC5; "
    "`cases/layer3-formal-trajectories.yaml`). Each is a tick grid (1s ticks) with a "
    "run-length user state (m=mid-utterance, b=breakpoint/pause, i=idle), a queue of held "
    "items (urgency / relevance / standing-order + §3.3 fields: supersedes, stale, privacy, "
    "retryable, condition, third_party, modality), and a per-tick ground-truth action.\n\n"
    "**Per tick** the model is shown the topic, the user's state, and the pending item(s), and "
    "emits a `NOW / WAIT / DROP` decision (+form `BRIEF|FULL|SILENT|CHIME` if NOW). The "
    "`monitor_stream` arm does this via an explicit silent `<monitor>` block (intervention 2b, "
    "docs/multi-stream-simulation-and-tact-bench.md). **Single-channel rule:** at most one NOW "
    "per tick — simultaneous NOWs serialize (higher urgency first).\n\n"
    "**Scoring.** The sparse gt expands deterministically: an item is WAIT from `t_avail` until "
    "its decisive tick, then its action, then DONE. Per-item outcome ∈ {correct, wrong_form, "
    "miss, cried_wolf}. Metrics — **action_accuracy** (deliver/suppress + timing right, form "
    "aside), **form_accuracy** (of on-time deliveries, form matches), **cried_wolf** (premature "
    "or should-drop ÷ deliveries; lower better), **urgent_miss** (high-urgency NOW not delivered "
    "by deadline; lower better). Reference columns: **oracle** = gt ceiling, **never** = "
    "always-silent floor."
)


def _repro_line(arms: list[str]) -> str:
    return (
        "**Reproduce.** `PYTHONPATH=. python scripts/run_tact_layer3.py --arms "
        + " ".join(arms) + "` (re-render from saved decisions: add `--render-only`). "
        "Scorer: `companion_harness/evals/adapters/tact_bench_layer3.py`; elicitation: "
        "`..._layer3_run.py`."
    )


def _setup_md(arms: list[str]) -> str:
    L = ["## Setup & background", "", _BACKGROUND, "",
         "**The arms** (the only thing that varies — same cases, same per-tick state, "
         "differ only by system prompt):", ""]
    for a in arms:
        L.append(f"<details><summary><b>{a}</b> system prompt</summary>")
        L.append("")
        L.append("```")
        L.append(_ARMS.get(a, "(unknown arm)"))
        L.append("```")
        L.append("</details>")
        L.append("")
    L += [_repro_line(arms), ""]
    return "\n".join(L)


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


def _load_descriptions() -> dict:
    """Plain-language case info joined from Layer 2 (title/topic/standing/payload/gt)."""
    import yaml
    from companion_harness.evals.adapters.tact_bench_layer3_run import _LAYER2

    raw = yaml.safe_load(_LAYER2.read_text())["scenarios"]
    out: dict[str, dict] = {}
    for c in raw:
        payloads = []
        if c.get("item"):
            payloads.append((c["item"]["id"], c["item"].get("payload", "")))
        for it in (c.get("items") or []):
            payloads.append((it["id"], it.get("payload", "")))
        out[c["id"]] = {
            "title": c.get("title", ""), "topic": c.get("topic", ""),
            "standing": c.get("standing_instruction"),
            "ground_truth": c.get("ground_truth", ""), "payloads": payloads,
        }
    return out


def _describe(d: dict) -> str:
    if not d:
        return ""
    parts = []
    if d.get("title"):
        parts.append(f"*{d['title']}.*")
    s = f"User context: {d['topic']}" if d.get("topic") else "User context: —"
    if d.get("standing"):
        s += f"; earlier the user said: {d['standing']}"
    parts.append(s + ".")
    if d.get("payloads"):
        parts.append("Held: " + "; ".join(f'{pid} = "{p}"' for pid, p in d["payloads"]) + ".")
    if d.get("ground_truth"):
        parts.append(f"Correct: {d['ground_truth']}")
    return " ".join(parts)


def _inline_html(s: str) -> str:
    import html as _h
    s = _h.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"\*(.+?)\*", r"<i>\1</i>", s)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    return s


def _md_to_html(text: str) -> str:
    blocks = []
    for para in (text or "").split("\n\n"):
        para = para.strip()
        if not para:
            continue
        lines = para.splitlines()
        if all(ln.lstrip().startswith("- ") for ln in lines):
            blocks.append("<ul>" + "".join(f"<li>{_inline_html(ln.lstrip()[2:])}</li>" for ln in lines) + "</ul>")
        else:
            blocks.append(f"<p>{_inline_html(' '.join(lines))}</p>")
    return "\n".join(blocks)


def _md(cols, scores, cases, arms, decisions, meta, descriptions=None, verdict="") -> str:
    descriptions = descriptions or {}
    L = [f"# TACT-Bench Layer-3 — mechanical scorer (no LLM judge)", "",
         f"- Model: `{meta['model']}` | cases: {len(cases)} | arms: {', '.join(arms)} | date: {meta['date']}",
         "- Scoring is deterministic (per-tick gt); columns include the gt **oracle** (ceiling) "
         "and **never-deliver** (floor) for reference.", "",
         _setup_md(arms),
         "## Metrics", "", "| metric | direction | " + " | ".join(cols) + " |",
         "|---|---|" + "---|" * len(cols)]
    for n, d in _METRICS:
        L.append(f"| {n} | {d} | " + " | ".join(_fmt(scores[c].get(n)) for c in cols) + " |")
    L += ["", "## Outcome counts (per arm)", "",
          "| arm | correct | wrong_form | miss | cried_wolf |", "|---|---|---|---|---|"]
    for a in arms:
        o = scores[a]["outcomes"]
        L.append(f"| {a} | {o.get('correct',0)} | {o.get('wrong_form',0)} | {o.get('miss',0)} | {o.get('cried_wolf',0)} |")
    if verdict:
        L += ["", "## Verdict", "", verdict]
    L += ["", "## Per-case detail (each case in plain language; then gt vs each arm: action@tick [outcome])", ""]
    for c in cases:
        L.append(f"### {c.id} — {c.ticks} ticks")
        desc = _describe(descriptions.get(c.id, {}))
        if desc:
            L += [f"> {desc}", ""]
        for it in c.items:
            exp = f"NOW:{it.expected.form}" if it.expected.kind == "NOW" else it.expected.kind
            L.append(f"- **{it.id}** (u={it.u} r={it.r} s={it.s}, t_avail={it.t_avail}) — "
                     f"gt: **{exp}@{it.decisive_tick}**")
            for a in arms:
                L.append(f"    - {a}: {_outcome_str(c, it.id, decisions[a])}")
        L.append("")
    return "\n".join(L)


def _html(cols, scores, cases, arms, decisions, meta, descriptions=None, verdict="") -> str:
    import html
    descriptions = descriptions or {}
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
        desc = _describe(descriptions.get(c.id, {}))
        desc_html = f"<p class='casedesc'>{_inline_html(desc)}</p>" if desc else ""
        blocks.append(f"<div class='case'><h3>{c.id} — {c.ticks} ticks</h3>{desc_html}{''.join(items_html)}</div>")
    verdict_html = (f"<h2>Verdict</h2>{_md_to_html(verdict)}" if verdict else "")
    setup = (
        "<h2>Setup &amp; background</h2>"
        "<p>TACT-Bench scores <i>when an always-on assistant should surface a result it already "
        "holds</i> on its single audio channel (deliver / defer / drop, and in what form), with "
        "staying silent a first-class correct action. <b>Layer 3</b> is the formal per-tick ground "
        "truth, scored <b>mechanically (no LLM judge)</b> — deterministic and reproducible.</p>"
        "<p><b>Cases:</b> 28 model-agnostic scenarios (TC1–TC29, no TC5). Each is a 1s tick grid "
        "with a user state (m=mid-utterance, b=pause, i=idle), a queue of held items "
        "(urgency/relevance/standing + §3.3: supersedes, stale, privacy, retryable, condition, "
        "third_party, modality), and a per-tick gt action. Per tick the model emits NOW/WAIT/DROP "
        "(+form BRIEF|FULL|SILENT|CHIME); the <code>monitor_stream</code> arm uses an explicit "
        "silent &lt;monitor&gt; block (intervention 2b). Single-channel rule: ≤1 NOW per tick.</p>"
        "<p><b>Metrics:</b> action_accuracy (deliver/suppress + timing right, form aside); "
        "form_accuracy (of on-time deliveries); cried_wolf (premature/should-drop ÷ deliveries; "
        "lower better); urgent_miss (high-urgency NOW missed; lower better). <b>oracle</b> = gt "
        "ceiling, <b>never</b> = silence floor.</p>"
        "<p><b>The arms</b> (same cases, differ only by system prompt):</p>"
        + "".join(
            f"<details><summary><b>{a}</b> system prompt</summary>"
            f"<pre class='prompt'>{html.escape(_ARMS.get(a, '?'))}</pre></details>"
            for a in arms
        )
        + f"<p><b>Reproduce:</b> <code>PYTHONPATH=. python scripts/run_tact_layer3.py --arms "
        f"{' '.join(arms)}</code> (re-render: <code>--render-only</code>).</p>"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>TACT-Bench Layer-3</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:960px;margin:2rem auto;color:#1a1a1a}}
 h1{{font-size:1.5rem}} h2{{font-size:1.15rem;border-bottom:1px solid #ddd;margin-top:1.6rem}}
 table{{border-collapse:collapse;width:100%;font-size:.9rem}} th,td{{border:1px solid #ddd;padding:.35rem .5rem;text-align:center}}
 th{{background:#f4f6f8}} td.m{{text-align:left;font-family:ui-monospace,monospace}} td.dir{{color:#777;font-size:.82rem}}
 .meta{{color:#555;font-size:.9rem}} img{{max-width:100%;border:1px solid #eee}}
 .case{{margin:.7rem 0;padding:.4rem .8rem;border:1px solid #eee;border-radius:6px}} .case h3{{font-size:1rem;margin:.2rem 0}}
 .desc{{margin:.3rem 0 .1rem;color:#333}} .arms{{margin:.1rem 0 .4rem 1rem}} code{{font-size:.85em}}
 .casedesc{{margin:.2rem 0 .5rem;color:#555;font-size:.88rem;background:#fafafa;border-left:3px solid #ddd;padding:.3rem .6rem}}
 pre.prompt{{background:#f7f7f8;border:1px solid #eee;padding:.5rem .7rem;white-space:pre-wrap;font-size:.82rem;border-radius:4px}}
 details{{margin:.3rem 0}} summary{{cursor:pointer}}
</style></head><body>
<h1>TACT-Bench Layer-3 — mechanical scorer (no LLM judge)</h1>
<p class="meta">Model <code>{meta['model']}</code> · {len(cases)} cases · arms: {', '.join(arms)} · {meta['date']}<br>
Deterministic per-tick scoring; <b>oracle</b> = gt ceiling, <b>never</b> = silence floor.</p>
{setup}
<h2>Metrics</h2>
<table><thead><tr><th>metric</th><th>direction</th>{head}</tr></thead><tbody>{mrows}</tbody></table>
<img src="data:image/png;base64,{chart}">
{verdict_html}
<h2>Per-case detail <span class="meta">(each case in plain language; then gt vs each arm: action@tick [outcome])</span></h2>
{''.join(blocks)}
<footer style="color:#888;font-size:.8rem;margin-top:2rem">Generated by scripts/run_tact_layer3.py ·
scorer: companion_harness/evals/adapters/tact_bench_layer3.py</footer>
</body></html>
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--arms", nargs="+", default=["vanilla", "prompted", "monitor_stream"])
    p.add_argument("--output", default="reports/tact-layer3")
    p.add_argument("--model", default="minicpm",
                   help="minicpm | gpt-realtime-2 (or any OpenAI chat model id)")
    p.add_argument("--limit", type=int, default=0, help="run only the first N cases (0 = all; smoke test)")
    p.add_argument("--render-only", action="store_true", dest="render_only",
                   help="re-render report.md/html from saved decisions.json (no model run)")
    args = p.parse_args()

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    cases = load_layer3()
    if args.limit:
        cases = cases[: args.limit]
    descriptions = _load_descriptions()
    # Verdict is interpretive prose; keep it in verdict.md so re-renders preserve it.
    verdict = (out / "verdict.md").read_text() if (out / "verdict.md").exists() else ""

    if args.render_only:
        decisions = json.loads((out / "decisions.json").read_text())
        cols = list(decisions.keys())
        arms = [c for c in cols if c not in ("oracle", "never")]
        scores = {c: score_all(cases, decisions[c]) for c in cols}
        # Read the model label persisted by the live run; fall back to the flag/default
        # so a GPT re-render is not mislabelled as MiniCPM.
        meta = _read_meta(out, args.model)
        (out / "report.md").write_text(_md(cols, scores, cases, arms, decisions, meta, descriptions, verdict))
        (out / "report.html").write_text(_html(cols, scores, cases, arms, decisions, meta, descriptions, verdict))
        (out / "scores.json").write_text(json.dumps(scores, indent=2))
        print(f"re-rendered {out}/report.md, report.html, scores.json (no model)")
        return 0

    ctx = load_context()
    model_factory = _shared_model_factory(args.model)

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
    meta = {"model": _MODEL_LABELS.get(args.model, args.model), "date": date.today().isoformat()}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))  # so --render-only labels correctly
    (out / "report.md").write_text(_md(cols, scores, cases, args.arms, decisions, meta, descriptions, verdict))
    (out / "report.html").write_text(_html(cols, scores, cases, args.arms, decisions, meta, descriptions, verdict))
    (out / "scores.json").write_text(json.dumps({c: scores[c] for c in cols}, indent=2))
    (out / "decisions.json").write_text(json.dumps(decisions, indent=2))
    print(f"\nWrote {out}/report.md, report.html, scores.json, decisions.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
