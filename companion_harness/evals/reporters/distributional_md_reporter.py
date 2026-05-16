"""Distributional Markdown reporter (Phase B1).

Renders timing distributions as text-bar histograms in Markdown and compares
them against published human-conversation envelopes from the CANDOR paper
(Cao et al. 2023, https://doi.org/10.1126/sciadv.adf3197, §3.2–3.4).

Framing: "are our gaps/overlaps/backchannel pauses/response delays inside a
human-ish envelope?" — NOT "did we beat CANDOR?"

Phase A.5+1 introduces matplotlib HTML histograms; this reporter uses
text bars only (no matplotlib dependency).
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from companion_harness.evals.schemas import BenchmarkResult, MetricValue


# ---------------------------------------------------------------------------
# Human-normal envelopes (CANDOR paper §3.2–3.4)
# Published values used for "inside human envelope?" framing.
# ---------------------------------------------------------------------------

_ENVELOPES: dict[str, dict] = {
    "turn_gap_ms_distribution": {
        "label": "Turn gap",
        "human_median_ms": 200,
        "human_p95_ms": 800,
        "note": "CANDOR §3.2: median ~200 ms, p95 ~800 ms",
    },
    "overlap_ms_distribution": {
        "label": "Overlap",
        "human_median_ms": 150,
        "human_p95_ms": 500,
        "note": "CANDOR §3.3: median ~150 ms; > 500 ms rare in natural conversation",
    },
    "backchannel_pause_distribution": {
        "label": "Backchannel pause",
        "human_median_ms": 99,
        "human_p95_ms": 300,
        "note": "CANDOR §3.4: backchannels cluster < 300 ms after prosodic dip",
    },
    "response_delay_distribution": {
        "label": "Response delay",
        "human_median_ms": 300,
        "human_p95_ms": 1200,
        "note": "CANDOR §3.2+5: median ~300 ms, p95 ~1 200 ms; > 2 000 ms perceived as unnatural",
    },
}

_BAR_WIDTH = 30
_BAR_CHAR = "█"


def _text_bar(count: int, max_count: int) -> str:
    filled = round(_BAR_WIDTH * count / max_count) if max_count else 0
    return _BAR_CHAR * filled + "░" * (_BAR_WIDTH - filled)


def _render_histogram(value_dict: dict) -> list[str]:
    buckets: dict = value_dict.get("buckets", {})
    if not buckets:
        return ["  *(no data)*"]
    max_count = max(buckets.values()) or 1
    sorted_keys = sorted(buckets.keys(), key=lambda k: int(k))
    lines = []
    lines.append(f"  {'bucket (ms)':>14} | {'bar':^{_BAR_WIDTH}} | count")
    lines.append(f"  {'-'*14}-+-{'-'*_BAR_WIDTH}-+------")
    for key in sorted_keys:
        c = buckets[key]
        lines.append(f"  {key:>14} | {_text_bar(c, max_count)} | {c}")
    return lines


def _envelope_verdict(mv: MetricValue, envelope: dict) -> str:
    val = mv.value if isinstance(mv.value, dict) else {}
    p50 = val.get("p50_ms")
    p95 = val.get("p95_ms")
    if p50 is None:
        return "**no data**"
    inside_p50 = abs(p50 - envelope["human_median_ms"]) <= envelope["human_median_ms"] * 0.5
    inside_p95 = p95 is not None and p95 <= envelope["human_p95_ms"] * 1.5
    if inside_p50 and inside_p95:
        return "inside human-ish envelope"
    parts = []
    if not inside_p50:
        parts.append(f"p50 {p50:.0f} ms vs human {envelope['human_median_ms']} ms")
    if not inside_p95:
        parts.append(f"p95 {p95:.0f} ms vs human p95 {envelope['human_p95_ms']} ms")
    return "outside envelope — " + "; ".join(parts)


def _render_metric_section(mv: MetricValue) -> list[str]:
    envelope = _ENVELOPES.get(mv.name, {})
    label = envelope.get("label", mv.name)
    lines = [f"### {label} (`{mv.name}`)"]
    if envelope:
        lines.append(f"")
        lines.append(f"> **Human envelope ({envelope['note']})**")
        lines.append(f"> median ≈ {envelope['human_median_ms']} ms, p95 ≈ {envelope['human_p95_ms']} ms")
    lines.append("")
    val = mv.value if isinstance(mv.value, dict) else {}
    count = val.get("count", 0)
    mean = val.get("mean_ms")
    p50 = val.get("p50_ms")
    p95 = val.get("p95_ms")
    lines.append(f"**Observed:** n={count}, mean={mean} ms, p50={p50} ms, p95={p95} ms")
    if envelope:
        verdict = _envelope_verdict(mv, envelope)
        lines.append(f"**Envelope verdict:** {verdict}")
    lines.append("")
    lines.append("**Histogram:**")
    lines.append("")
    lines.extend(_render_histogram(val))
    lines.append("")
    return lines


def _collect_merged_metrics(results: Sequence[BenchmarkResult]) -> dict[str, MetricValue]:
    """Merge per-case MetricValues by summing histogram buckets across all cases."""
    merged: dict[str, dict] = {}
    merged_meta: dict[str, MetricValue] = {}
    for result in results:
        for mv in result.metrics:
            if not isinstance(mv.value, dict) or mv.aggregation != "distribution":
                continue
            val = mv.value
            if mv.name not in merged:
                merged[mv.name] = {
                    "buckets": {},
                    "count": 0,
                    "mean_sum": 0.0,
                    "values": [],
                }
                merged_meta[mv.name] = mv
            m = merged[mv.name]
            for bucket_key, c in val.get("buckets", {}).items():
                m["buckets"][bucket_key] = m["buckets"].get(bucket_key, 0) + c
            if val.get("count"):
                m["count"] += val["count"]
                if val.get("mean_ms") is not None:
                    m["mean_sum"] += val["mean_ms"] * val["count"]

    out: dict[str, MetricValue] = {}
    for name, m in merged.items():
        ref = merged_meta[name]
        n = m["count"]
        mean = round(m["mean_sum"] / n, 1) if n else None
        all_vals: list[float] = []
        for bucket_key, c in m["buckets"].items():
            all_vals.extend([float(bucket_key)] * c)
        all_vals.sort()
        p50 = round(all_vals[int(len(all_vals) * 0.50)], 1) if all_vals else None
        p95 = round(all_vals[min(int(len(all_vals) * 0.95), len(all_vals) - 1)], 1) if all_vals else None
        out[name] = MetricValue(
            name=name,
            value={"buckets": m["buckets"], "count": n, "mean_ms": mean, "p50_ms": p50, "p95_ms": p95},
            unit=ref.unit,
            aggregation="distribution",
        )
    return out


class DistributionalMdReporter:
    """Renders CANDOR distributional results as Markdown with text histograms.

    Compares observed distributions against published CANDOR human-conversation
    envelopes. Answers: "are our gaps/overlaps/backchannel pauses/response
    delays inside a human-ish envelope?" — not "did we beat CANDOR?"
    """

    def render(self, results: list[BenchmarkResult], output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        report_path = output_dir / "distributional_report.md"

        is_synthetic = any(
            r.final_status == "completed" for r in results
        )
        synthetic_label = " [synthetic]" if is_synthetic else ""

        lines: list[str] = [
            f"# CANDOR Distributional Probe{synthetic_label}",
            "",
            "> **Framing:** Are our timing gaps, overlaps, backchannel pauses, and"
            " response delays inside a human-ish envelope?",
            "> NOT: Did we beat CANDOR?",
            "",
            f"Reference: Cao et al. 2023, *CANDOR: A Large Scale Dataset of"
            " Conversations in the Wild*,",
            "https://doi.org/10.1126/sciadv.adf3197",
            "",
        ]
        if is_synthetic:
            lines += [
                "> **Synthetic mode:** No real CANDOR audio loaded. 100 utterance pairs"
                " generated using log-normal timing distributions calibrated to",
                "> CANDOR paper §3.2–3.4 envelopes. Results are representative of the"
                " expected human-conversation shape.",
                "",
            ]

        lines += [
            f"Cases evaluated: {len(results)}",
            "",
            "---",
            "",
            "## Per-metric distributional summary",
            "",
        ]

        merged = _collect_merged_metrics(results)
        for metric_name in [
            "turn_gap_ms_distribution",
            "overlap_ms_distribution",
            "backchannel_pause_distribution",
            "response_delay_distribution",
        ]:
            if metric_name in merged:
                lines.extend(_render_metric_section(merged[metric_name]))

        report_path.write_text("\n".join(lines), encoding="utf-8")
