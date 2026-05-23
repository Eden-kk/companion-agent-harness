"""Runner: TACT-Bench vanilla-vs-prompted pilot on MiniCPM-o duplex.

Run on B200 (this machine):

    /raid/yid042/venvs/companion-harness/bin/python \\
        scripts/probe_tact_vanilla_vs_prompted.py

Drives the 4 scenarios in
``tact-bench/experiments/vanilla-vs-prompted/scenarios.yaml`` through MiniCPM-o
4.5 in duplex mode, twice each (vanilla vs. prompted system prompt, identical
audio + identical held-result injection), reading the model's native is_listen /
text gate per 1-second chunk (Mode B, the native gate).

This is the *data-collection* runner. It produces the headline artifact — paired
per-chunk transcripts for each scenario/arm — plus a raw trajectories JSON. The
LLM-judge + 4-metric scoring is wired as a seam (``--judge``) that degrades
cleanly to "PENDING (needs judge)" when no judge is available, so the runner is
useful before the judge exists.

Pipeline per (scenario, arm):
  1. reset_session + prepare(prefix_system_prompt=<arm prompt>)
  2. render user_script -> 16kHz audio timeline (Kokoro TTS); pauses = silence
  3. step in 1s chunks; at item.t_available inject the held result as a private
     ChatML *system turn* via streaming_prefill(text_list=...) so the model treats
     it as context, not speakable stream content (see REVISIONS R1 in tact-bench
     and probe_tact_pending_injection.py)
  4. log per chunk: t, is_listen, text

Outputs (default into the tact-bench results dir):
  - results-<date>.md      filled vanilla-vs-prompted table + paired transcripts
  - trajectories-<date>.json   raw per-chunk logs

The two system prompts are embedded verbatim from the experiment README so the
run is self-contained and reproducible.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 16000  # 1.0s
_TAIL_CHUNKS = 6        # trailing silence so deferred deliveries have room to land

_KOKORO_DEFAULT_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_DEFAULT_VOICES = "/raid/yid042/models/kokoro/voices.json"

_DEFAULT_SCENARIOS = Path(
    "/home/yid042/projects/tact-bench/experiments/vanilla-vs-prompted/scenarios.yaml"
)
_DEFAULT_OUTDIR = Path(
    "/home/yid042/projects/tact-bench/experiments/vanilla-vs-prompted/results"
)

_VANILLA_SYSTEM_PROMPT = "You are a helpful real-time voice assistant. Respond naturally to the user."

# Revised prompt (REVISIONS R1): no literal "[PENDING: ...]" surface form — the old
# wording taught the model to *emit* that bracketed tag. Held results now arrive as
# private system notes, with an explicit "never read the note aloud verbatim" rule.
_PROMPTED_SYSTEM_PROMPT = (
    "You are an always-on voice assistant sharing a single audio channel with the user.\n"
    "From time to time you privately receive a background result as a system note (for\n"
    "example a tool result, a finished task, or a reminder). A system note is NOT something\n"
    "the user said, and you must never read the note or its labels aloud verbatim — speak\n"
    "only the underlying information, and only when appropriate.\n\n"
    "For each background result, decide each moment:\n"
    "- DELIVER NOW only if it is urgent OR the user explicitly asked to be told the moment\n"
    "  it's ready. When you deliver mid-conversation, be brief.\n"
    "- DEFER (stay silent now, deliver at the next natural pause) if it is relevant but not\n"
    "  urgent. When you finally deliver a deferred item, re-anchor it (\"about that X you\n"
    "  asked earlier...\").\n"
    "- DROP (never mention it) if it is no longer relevant, the user already resolved it, or\n"
    "  it has gone stale.\n"
    "Do not interrupt the user mid-sentence for anything non-urgent. Staying silent is often\n"
    "the correct choice. Match length to urgency: urgent -> one short sentence; otherwise brief."
)

_ARMS = {"vanilla": _VANILLA_SYSTEM_PROMPT, "prompted": _PROMPTED_SYSTEM_PROMPT}


# --------------------------------------------------------------------------- #
# TTS
# --------------------------------------------------------------------------- #
def _load_kokoro() -> object | None:
    model_path = os.environ.get("KOKORO_MODEL_PATH", _KOKORO_DEFAULT_MODEL)
    voices_path = os.environ.get("KOKORO_VOICES_PATH", _KOKORO_DEFAULT_VOICES)
    if not Path(model_path).exists() or not Path(voices_path).exists():
        print(f"  warn: Kokoro not found at {model_path} — user audio will be SILENCE", flush=True)
        return None
    try:
        from companion_harness.tts_kokoro import KokoroTtsAdapter

        adapter = KokoroTtsAdapter(model_path=model_path, voices_path=voices_path, warmup=False)
        print(f"  Kokoro loaded from {model_path}", flush=True)
        return adapter
    except Exception as exc:  # noqa: BLE001
        print(f"  warn: Kokoro load failed ({type(exc).__name__}: {exc}) — SILENCE", flush=True)
        return None


def _synthesize_16k(kokoro: object | None, text: str) -> np.ndarray:
    if kokoro is None:
        return np.zeros(0, dtype=np.float32)

    async def _collect() -> np.ndarray:
        out: list[np.ndarray] = []
        async for pcm16 in kokoro.synthesize(text, []):  # type: ignore[attr-defined]
            out.append(np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        return np.concatenate(out) if out else np.zeros(0, dtype=np.float32)

    raw_24k = asyncio.new_event_loop().run_until_complete(_collect())
    if len(raw_24k) == 0:
        return np.zeros(0, dtype=np.float32)
    ratio = _SAMPLE_RATE / 24000.0
    out_len = int(len(raw_24k) * ratio)
    idx = np.clip(np.round(np.arange(out_len) / ratio).astype(int), 0, len(raw_24k) - 1)
    return raw_24k[idx]


# --------------------------------------------------------------------------- #
# Scenario -> audio timeline
# --------------------------------------------------------------------------- #
def _build_timeline(scenario: dict, kokoro: object | None) -> tuple[np.ndarray, int]:
    """Render user_script into a flat 16kHz buffer; return (buffer, n_chunks).

    Timing convention (scenarios.yaml): t is in 1s chunks; speaker entries place
    rendered speech starting at chunk t; pause entries reserve `dur` chunks of
    silence at chunk t.
    """
    placements: list[tuple[int, np.ndarray]] = []  # (start_sample, audio)
    max_end_sample = 0
    for entry in scenario["user_script"]:
        t = int(entry["t"])
        start = t * _CHUNK_SAMPLES
        if entry.get("type") == "pause":
            dur = int(entry.get("dur", 1))
            max_end_sample = max(max_end_sample, start + dur * _CHUNK_SAMPLES)
            continue
        audio = _synthesize_16k(kokoro, entry["text"])
        placements.append((start, audio))
        max_end_sample = max(max_end_sample, start + len(audio))

    item = scenario.get("item") or {}
    t_avail = int(item.get("t_available", 0))
    max_end_sample = max(max_end_sample, (t_avail + 1) * _CHUNK_SAMPLES)

    n_chunks = math.ceil(max_end_sample / _CHUNK_SAMPLES) + _TAIL_CHUNKS
    buf = np.zeros(n_chunks * _CHUNK_SAMPLES, dtype=np.float32)
    for start, audio in placements:
        end = min(start + len(audio), len(buf))
        buf[start:end] = audio[: end - start]
    return buf, n_chunks


# --------------------------------------------------------------------------- #
# Duplex stepping
# --------------------------------------------------------------------------- #
def _generate(duplex: object) -> dict:
    return duplex.streaming_generate(  # type: ignore[attr-defined]
        max_new_speak_tokens_per_chunk=duplex.max_new_speak_tokens_per_chunk,  # type: ignore[attr-defined]
        temperature=duplex.temperature,  # type: ignore[attr-defined]
        top_k=duplex.top_k,  # type: ignore[attr-defined]
        top_p=duplex.top_p,  # type: ignore[attr-defined]
        listen_prob_scale=duplex.listen_prob_scale,  # type: ignore[attr-defined]
        text_repetition_penalty=duplex.text_repetition_penalty,  # type: ignore[attr-defined]
        text_repetition_window_size=duplex.text_repetition_window_size,  # type: ignore[attr-defined]
    )


def _held_result_turn(item: dict) -> str:
    """Frame the held result as a private ChatML *system turn* (REVISIONS R1).

    The previous framing injected a bare "[PENDING: ...]" string into the
    generation stream with no role markers, so the model parroted the tag as
    speech. Wrapping it as a self-contained system turn (the model's own
    role-delimiter convention, recognized by the tokenizer) marks it as private
    context, and the inline note reinforces "do not read aloud verbatim".
    """
    payload = item.get("payload", "")
    urgent = (item.get("labels") or {}).get("urgency") == "high"
    result = f"URGENT: {payload}" if urgent else payload
    note = (
        "Background result now available (private system note — not user speech; "
        "do not read this note aloud verbatim). "
        f"Result: {result}."
    )
    return f"<|im_start|>system\n{note}<|im_end|>\n"


def _run_arm(
    model: object,
    scenario: dict,
    arm_prompt: str,
    buf: np.ndarray,
    n_chunks: int,
) -> list[dict]:
    duplex = model._duplex  # noqa: SLF001
    duplex.model.reset_session(reset_token2wav_cache=False)  # type: ignore[attr-defined]
    duplex.prepare(prefix_system_prompt=arm_prompt)  # type: ignore[attr-defined]

    item = scenario.get("item") or {}
    t_avail = int(item.get("t_available", -1))
    note = _held_result_turn(item)

    trajectory: list[dict] = []
    for t in range(n_chunks):
        chunk = buf[t * _CHUNK_SAMPLES : (t + 1) * _CHUNK_SAMPLES]
        injected = False
        if t == t_avail:
            duplex.streaming_prefill(audio_waveform=chunk, text_list=[note])  # type: ignore[attr-defined]
            injected = True
        else:
            duplex.streaming_prefill(audio_waveform=chunk)  # type: ignore[attr-defined]
        result = _generate(duplex)
        trajectory.append(
            {
                "t": t,
                "injected": injected,
                "is_listen": bool(result.get("is_listen", True)),
                "text": result.get("text", ""),
            }
        )
    return trajectory


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _spoken_text(trajectory: list[dict]) -> str:
    """Concatenate text from chunks where the model chose to speak."""
    parts = [c["text"].strip() for c in trajectory if not c["is_listen"] and c["text"].strip()]
    return " ".join(parts).strip()


def _format_transcript(trajectory: list[dict]) -> str:
    lines = []
    for c in trajectory:
        gate = "SPEAK " if not c["is_listen"] else "listen"
        mark = " <<INJECT" if c["injected"] else ""
        txt = c["text"].replace("\n", " ").strip()
        lines.append(f"  t={c['t']:>2} {gate} {txt!r}{mark}")
    return "\n".join(lines)


def _write_report(
    results: dict,
    scenarios: list[dict],
    outdir: Path,
    judged: bool,
    meta: dict,
) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    md = outdir / f"results-{today}.md"

    metric_cell = lambda v: (f"{v:.2f}" if isinstance(v, (int, float)) else "PENDING (needs judge)")

    lines: list[str] = []
    lines.append("# Results — vanilla vs. prompted MiniCPM-o pilot")
    lines.append("")
    lines.append("> Auto-generated by `scripts/probe_tact_vanilla_vs_prompted.py`.")
    lines.append("")
    lines.append("## Run metadata")
    lines.append("")
    lines.append(f"- Model / build: `{meta['model_id']}` ({meta['precision']})")
    lines.append("- API verified against released code? yes — `streaming_prefill(text_list=...)` "
                 "confirmed (modeling_minicpmo.py L2759/L3094); injection probed by "
                 "`probe_tact_pending_injection.py`")
    lines.append("- Mode(s) run: B (native gate)")
    lines.append("- Injection framing: held result wrapped as a private ChatML system turn "
                 "(REVISIONS R1; no bare [PENDING:] tag)")
    lines.append(f"- TTS used: {meta['tts']}")
    lines.append(f"- LLM judge used: {meta['judge'] if judged else 'NONE (metrics pending)'}")
    lines.append(f"- Date: {today}")
    lines.append("")
    lines.append("## Metric table")
    lines.append("")
    lines.append("| Metric | vanilla | prompted | expected direction |")
    lines.append("|---|---|---|---|")
    m = results.get("metrics", {})
    lines.append(f"| Cried-wolf rate | {metric_cell(m.get('vanilla',{}).get('cried_wolf'))} "
                 f"| {metric_cell(m.get('prompted',{}).get('cried_wolf'))} | prompted lower |")
    lines.append(f"| Urgent-miss rate | {metric_cell(m.get('vanilla',{}).get('urgent_miss'))} "
                 f"| {metric_cell(m.get('prompted',{}).get('urgent_miss'))} | both low |")
    lines.append(f"| Breakpoint-hit rate | {metric_cell(m.get('vanilla',{}).get('breakpoint_hit'))} "
                 f"| {metric_cell(m.get('prompted',{}).get('breakpoint_hit'))} | prompted higher |")
    lines.append(f"| Form accuracy | {metric_cell(m.get('vanilla',{}).get('form_acc'))} "
                 f"| {metric_cell(m.get('prompted',{}).get('form_acc'))} | prompted higher |")
    lines.append("")
    lines.append("## Per-scenario outcome")
    lines.append("")
    lines.append("| Scenario | Ground truth | vanilla spoke | prompted spoke |")
    lines.append("|---|---|---|---|")
    by_id = {s["id"]: s for s in scenarios}
    for sid, arms in results["scenarios"].items():
        gt = by_id.get(sid, {}).get("ground_truth", "")[:60]
        v = _spoken_text(arms["vanilla"]) or "(silent)"
        p = _spoken_text(arms["prompted"]) or "(silent)"
        lines.append(f"| {sid} | {gt} | {v[:60]} | {p[:60]} |")
    lines.append("")
    lines.append("## Paired transcripts")
    lines.append("")
    for sid, arms in results["scenarios"].items():
        lines.append(f"### {sid}")
        gt = by_id.get(sid, {}).get("ground_truth", "")
        lines.append(f"_ground truth:_ {gt}")
        lines.append("```")
        lines.append("vanilla:")
        lines.append(_format_transcript(arms["vanilla"]))
        lines.append("")
        lines.append("prompted:")
        lines.append(_format_transcript(arms["prompted"]))
        lines.append("```")
        lines.append("")
    lines.append("## Verdict (one paragraph)")
    lines.append("")
    lines.append("_TODO: fill after reviewing transcripts / metrics._")
    lines.append("")

    md.write_text("\n".join(lines))
    return md


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenarios", type=Path, default=_DEFAULT_SCENARIOS)
    parser.add_argument("--outdir", type=Path, default=_DEFAULT_OUTDIR)
    parser.add_argument("--limit", type=int, default=0, help="run only the first N scenarios (0=all)")
    parser.add_argument("--judge", action="store_true", help="run the LLM judge + metrics (seam; not yet implemented)")
    args = parser.parse_args()

    try:
        import yaml
    except ImportError:
        print("ERROR: PyYAML required (pip install pyyaml)", file=sys.stderr)
        return 1

    if not args.scenarios.exists():
        print(f"ERROR: scenarios file not found: {args.scenarios}", file=sys.stderr)
        return 1

    scenarios = yaml.safe_load(args.scenarios.read_text())["scenarios"]
    if args.limit > 0:
        scenarios = scenarios[: args.limit]
    print(f"Loaded {len(scenarios)} scenarios from {args.scenarios}", flush=True)

    print("Loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    model = MiniCPMStreamingModel()
    print(f"  model loaded in {round((time.monotonic() - t0) * 1000)}ms", flush=True)

    kokoro = _load_kokoro()

    results: dict = {"scenarios": {}, "metrics": {}}
    for scenario in scenarios:
        sid = scenario["id"]
        print(f"\n=== {sid} ===", flush=True)
        buf, n_chunks = _build_timeline(scenario, kokoro)
        print(f"  timeline: {n_chunks} chunks ({n_chunks}s)", flush=True)
        results["scenarios"][sid] = {}
        for arm, prompt in _ARMS.items():
            print(f"  arm={arm} ...", flush=True)
            traj = _run_arm(model, scenario, prompt, buf, n_chunks)
            results["scenarios"][sid][arm] = traj
            spoke = _spoken_text(traj) or "(silent)"
            print(f"    spoke: {spoke[:80]!r}", flush=True)

    judged = False
    if args.judge:
        # Seam for the Mode-B LLM delivery judge + 4-metric scoring.
        # Implement: per scenario/arm, label each spoken chunk for
        # delivered?/form, then compute cried-wolf / urgent-miss /
        # breakpoint-hit / form-accuracy against scenarios.yaml ground_truth.
        print("\n--judge requested but the judge is not yet implemented; "
              "emitting transcripts with PENDING metrics.", flush=True)

    meta = {
        "model_id": "openbmb/MiniCPM-o-4_5",
        "precision": "bf16",
        "tts": "Kokoro v0_19" if kokoro is not None else "SILENCE (no Kokoro)",
        "judge": "n/a",
    }
    md = _write_report(results, scenarios, args.outdir, judged, meta)

    today = date.today().isoformat()
    traj_path = args.outdir / f"trajectories-{today}.json"
    traj_path.write_text(json.dumps(results, indent=2))

    print(f"\nWrote report:       {md}")
    print(f"Wrote trajectories: {traj_path}")
    if not judged:
        print("\nNOTE: metrics are PENDING — wire the LLM judge (--judge seam) to fill them. "
              "Paired transcripts are complete and are the headline artifact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
