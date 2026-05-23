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
# Judge (Mode-B delivery detection) + metric scoring
# --------------------------------------------------------------------------- #
def _user_speaking_mask(buf: np.ndarray, n_chunks: int, thresh: float = 1e-4) -> list[bool]:
    """Per-chunk bool: is the *user* speaking (vs. a breakpoint/silence)?

    Derived from the actual rendered user audio energy — the same signal the
    model hears. Silence chunks are zero-filled, so a tiny RMS threshold cleanly
    separates speech from breakpoints.
    """
    mask: list[bool] = []
    for t in range(n_chunks):
        seg = buf[t * _CHUNK_SAMPLES : (t + 1) * _CHUNK_SAMPLES]
        rms = float(np.sqrt(np.mean(seg.astype(np.float64) ** 2))) if len(seg) else 0.0
        mask.append(rms > thresh)
    return mask


def _build_masks(scenarios: list[dict], kokoro: object | None) -> dict[str, list[bool]]:
    masks: dict[str, list[bool]] = {}
    for s in scenarios:
        buf, n = _build_timeline(s, kokoro)
        masks[s["id"]] = _user_speaking_mask(buf, n)
    return masks


def _first_breakpoint_at_or_after(mask: list[bool], t: int) -> "int | None":
    for i in range(max(t, 0), len(mask)):
        if not mask[i]:
            return i
    return None


_JUDGE_SYSTEM = (
    "You are a strict evaluator of a real-time voice assistant that shares one audio "
    "channel with a user. The assistant also privately received a HELD RESULT (a "
    "background fact) at a known moment and may speak it, defer it, or stay silent. "
    "Given the user's speech and the assistant's per-chunk spoken text, decide whether "
    "and how the assistant DELIVERED the held result's information.\n\n"
    "Rules:\n"
    "- 'Delivered' = the assistant conveyed the HELD RESULT's actual information to the "
    "user (paraphrase counts).\n"
    "- The assistant merely repeating or continuing the USER's own words is NOT a "
    "delivery — set echo_only=true if its speech is just parroting the user.\n"
    "- first_delivery_chunk = the integer t of the first assistant chunk that conveys "
    "the held result, or null.\n"
    "- form = 'BRIEF' (a single short sentence/phrase) or 'FULL' (multi-sentence / "
    "verbose) for the delivery, else null.\n"
    "Return ONLY a JSON object with keys: delivered (bool), first_delivery_chunk "
    "(int|null), form ('BRIEF'|'FULL'|null), echo_only (bool), rationale (string, "
    "<=25 words)."
)


def _judge_delivery(client: object, model: str, scenario: dict, trajectory: list[dict]) -> dict:
    item = scenario.get("item") or {}
    payload = item.get("payload", "")
    urgency = (item.get("labels") or {}).get("urgency", "low")
    t_avail = int(item.get("t_available", -1))

    user_lines = [
        f"  t={e['t']}: {e['text']}"
        for e in scenario.get("user_script", [])
        if e.get("speaker") == "user" and e.get("text")
    ] or ["  (none)"]
    spoke = [(c["t"], c["text"].strip()) for c in trajectory if not c["is_listen"] and c["text"].strip()]
    spoke_lines = [f"  t={t}: {txt}" for t, txt in spoke] or ["  (the assistant never spoke)"]

    user_msg = (
        f"HELD RESULT (urgency={urgency}): {payload}\n"
        f"The held result became available at chunk t={t_avail}.\n\n"
        "USER said:\n" + "\n".join(user_lines) + "\n\n"
        "ASSISTANT spoke (only chunks where it spoke):\n" + "\n".join(spoke_lines) + "\n\n"
        "Did the assistant deliver the held result? Respond as specified."
    )

    resp = client.chat.completions.create(  # type: ignore[attr-defined]
        model=model,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
    )
    data = json.loads(resp.choices[0].message.content)
    fdc = data.get("first_delivery_chunk")
    return {
        "delivered": bool(data.get("delivered", False)),
        "first_delivery_chunk": fdc if isinstance(fdc, int) else None,
        "form": data.get("form") if data.get("form") in ("BRIEF", "FULL") else None,
        "echo_only": bool(data.get("echo_only", False)),
        "rationale": str(data.get("rationale", ""))[:200],
    }


def _exposes(scenario: dict) -> list[str]:
    return [str(e) for e in (scenario.get("exposes") or [])]


def _compute_metrics(
    scenarios: list[dict],
    sids: list[str],
    labels: dict,
    masks: dict[str, list[bool]],
) -> dict:
    """Compute the 4 pilot metrics per arm from judge labels + ground truth.

    Scenario selection follows scenarios.yaml `exposes` tags. A delivery only
    "counts" if first_delivery_chunk >= item.t_available (the model cannot
    legitimately deliver a result before it exists; earlier matches are phantom
    echoes/anticipation and are excluded).
    """
    by_id = {s["id"]: s for s in scenarios}
    out: dict = {"vanilla": {}, "prompted": {}}

    for arm in ("vanilla", "prompted"):
        # 1. Cried-wolf: premature/unwanted deliveries ÷ deliveries (DEFER + DROP).
        cw_total = cw_unwanted = 0
        for sid in sids:
            s = by_id[sid]
            if not any(e.startswith("cried-wolf") for e in _exposes(s)):
                continue
            item = s.get("item") or {}
            tav = int(item.get("t_available", -1))
            lbl = labels[sid][arm]
            fdc = lbl["first_delivery_chunk"]
            if not (lbl["delivered"] and fdc is not None):
                continue
            cw_total += 1
            if s.get("behavior") == "DROP":
                cw_unwanted += 1  # should never have surfaced
            elif s.get("behavior") == "DEFER":
                bp = _first_breakpoint_at_or_after(masks[sid], tav)
                if bp is None or fdc < bp:
                    cw_unwanted += 1  # delivered before the correct breakpoint
        out[arm]["cried_wolf"] = (cw_unwanted / cw_total) if cw_total else 0.0
        out[arm]["cried_wolf_n"] = cw_total

        # 2. Urgent-miss: urgent item not delivered by its staleness deadline.
        um_total = um_miss = 0
        for sid in sids:
            s = by_id[sid]
            if "urgent-miss" not in _exposes(s):
                continue
            um_total += 1
            item = s.get("item") or {}
            tav = int(item.get("t_available", -1))
            stale = item.get("becomes_stale_at")
            lbl = labels[sid][arm]
            fdc = lbl["first_delivery_chunk"]
            valid = (
                lbl["delivered"]
                and fdc is not None
                and fdc >= tav
                and (not isinstance(stale, int) or fdc <= stale)
            )
            if not valid:
                um_miss += 1
        out[arm]["urgent_miss"] = (um_miss / um_total) if um_total else None

        # 3. Breakpoint-hit: of deliveries that should defer, fraction at a breakpoint.
        bh_deliv = bh_hit = 0
        for sid in sids:
            s = by_id[sid]
            if "breakpoint-hit" not in _exposes(s):
                continue
            item = s.get("item") or {}
            tav = int(item.get("t_available", -1))
            lbl = labels[sid][arm]
            fdc = lbl["first_delivery_chunk"]
            if lbl["delivered"] and fdc is not None and fdc >= tav:
                bh_deliv += 1
                mask = masks[sid]
                if 0 <= fdc < len(mask) and not mask[fdc]:
                    bh_hit += 1
        out[arm]["breakpoint_hit"] = (bh_hit / bh_deliv) if bh_deliv else None
        out[arm]["breakpoint_hit_n"] = bh_deliv

        # 4. Form accuracy: delivered form matches expected (BRIEF) on form scenarios.
        fa_total = fa_correct = 0
        for sid in sids:
            s = by_id[sid]
            if not any(e in ("form", "form-accuracy") for e in _exposes(s)):
                continue
            fa_total += 1
            item = s.get("item") or {}
            tav = int(item.get("t_available", -1))
            lbl = labels[sid][arm]
            fdc = lbl["first_delivery_chunk"]
            valid = lbl["delivered"] and fdc is not None and fdc >= tav
            if valid and lbl["form"] == "BRIEF":
                fa_correct += 1
        out[arm]["form_acc"] = (fa_correct / fa_total) if fa_total else None

    return out


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
    labels: "dict | None" = None,
) -> Path:
    outdir.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    md = outdir / f"results-{today}.md"

    def metric_cell(v: object) -> str:
        if isinstance(v, (int, float)):
            return f"{v:.2f}"
        return "n/a" if judged else "PENDING (needs judge)"

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
    if judged:
        lines.append("**Scoring method.** Scenario selection follows `scenarios.yaml` `exposes` "
                     "tags. Cried-wolf = premature/unwanted deliveries ÷ deliveries over DEFER+DROP "
                     "cases (premature = before the first user breakpoint at/after `t_available`; "
                     "DROP = any delivery). Urgent-miss = urgent item not delivered by "
                     "`becomes_stale_at`. Breakpoint-hit = of deliveries in DEFER cases, fraction "
                     "landing on a user breakpoint. Form accuracy = delivered form == BRIEF on form "
                     "cases. A delivery only counts if `first_delivery_chunk >= t_available` "
                     "(earlier matches are phantom echoes). `n/a` = no qualifying delivery to score.")
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

    if judged and labels:
        lines.append("## Judge detail (Mode-B delivery labels)")
        lines.append("")
        lines.append("| Scenario | Arm | delivered | chunk | form | echo-only | rationale |")
        lines.append("|---|---|---|---|---|---|---|")
        for sid in results["scenarios"]:
            for arm in ("vanilla", "prompted"):
                lb = labels[sid][arm]
                lines.append(
                    f"| {sid} | {arm} | {lb['delivered']} | "
                    f"{lb['first_delivery_chunk'] if lb['first_delivery_chunk'] is not None else '—'} | "
                    f"{lb['form'] or '—'} | {lb['echo_only']} | {lb['rationale']} |"
                )
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
    parser.add_argument("--judge", choices=["none", "openai"], default="none",
                        help="run the LLM delivery judge + fill metrics (needs OPENAI_API_KEY)")
    parser.add_argument("--judge-model", default="gpt-4o", help="OpenAI model for the judge")
    parser.add_argument("--score-only", action="store_true",
                        help="skip MiniCPM; load --trajectories and (re)judge/score them")
    parser.add_argument("--trajectories", type=Path, default=None,
                        help="trajectories JSON to score in --score-only mode")
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
    by_id = {s["id"]: s for s in scenarios}
    print(f"Loaded {len(scenarios)} scenarios from {args.scenarios}", flush=True)

    kokoro = _load_kokoro()

    # ---- collect trajectories (or load them) + per-scenario user-speaking masks
    if args.score_only:
        if not args.trajectories or not args.trajectories.exists():
            print("ERROR: --score-only needs an existing --trajectories file", file=sys.stderr)
            return 1
        results = json.loads(args.trajectories.read_text())
        results.setdefault("metrics", {})
        scenarios = [s for s in scenarios if s["id"] in results["scenarios"]]
        print(f"Loaded trajectories for {len(results['scenarios'])} scenarios "
              f"from {args.trajectories}", flush=True)
        masks = _build_masks(scenarios, kokoro)
    else:
        print("Loading MiniCPMStreamingModel...", flush=True)
        t0 = time.monotonic()
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

        model = MiniCPMStreamingModel()
        print(f"  model loaded in {round((time.monotonic() - t0) * 1000)}ms", flush=True)

        results = {"scenarios": {}, "metrics": {}}
        masks = {}
        for scenario in scenarios:
            sid = scenario["id"]
            print(f"\n=== {sid} ===", flush=True)
            buf, n_chunks = _build_timeline(scenario, kokoro)
            masks[sid] = _user_speaking_mask(buf, n_chunks)
            print(f"  timeline: {n_chunks} chunks ({n_chunks}s)", flush=True)
            results["scenarios"][sid] = {}
            for arm, prompt in _ARMS.items():
                print(f"  arm={arm} ...", flush=True)
                traj = _run_arm(model, scenario, prompt, buf, n_chunks)
                results["scenarios"][sid][arm] = traj
                print(f"    spoke: {(_spoken_text(traj) or '(silent)')[:80]!r}", flush=True)

    # ---- judge + score
    judged = False
    labels: dict | None = None
    if args.judge == "openai":
        if not os.environ.get("OPENAI_API_KEY"):
            print("\nWARNING: --judge openai but OPENAI_API_KEY is unset; metrics stay PENDING.",
                  file=sys.stderr)
        else:
            from openai import OpenAI

            client = OpenAI()
            print(f"\nJudging deliveries with OpenAI {args.judge_model}...", flush=True)
            labels = {}
            for sid in results["scenarios"]:
                labels[sid] = {}
                for arm in ("vanilla", "prompted"):
                    lbl = _judge_delivery(client, args.judge_model, by_id[sid], results["scenarios"][sid][arm])
                    labels[sid][arm] = lbl
                    print(f"  {sid}/{arm}: delivered={lbl['delivered']} "
                          f"chunk={lbl['first_delivery_chunk']} form={lbl['form']} "
                          f"echo={lbl['echo_only']}", flush=True)
            results["metrics"] = _compute_metrics(scenarios, list(results["scenarios"].keys()), labels, masks)
            results["judge_labels"] = labels
            judged = True

    meta = {
        "model_id": "openbmb/MiniCPM-o-4_5",
        "precision": "bf16",
        "tts": "Kokoro v0_19" if kokoro is not None else "SILENCE (no Kokoro)",
        "judge": f"OpenAI {args.judge_model}" if judged else "n/a",
    }
    md = _write_report(results, scenarios, args.outdir, judged, meta, labels)

    today = date.today().isoformat()
    traj_path = args.outdir / f"trajectories-{today}.json"
    traj_path.write_text(json.dumps(results, indent=2))
    if judged:
        (args.outdir / f"judge-labels-{today}.json").write_text(json.dumps(labels, indent=2))

    print(f"\nWrote report:       {md}")
    print(f"Wrote trajectories: {traj_path}")
    if judged:
        print(f"Wrote judge labels: {args.outdir / f'judge-labels-{today}.json'}")
    else:
        print("\nNOTE: metrics PENDING — re-run with `--judge openai` (and OPENAI_API_KEY set) "
              "to fill them. Paired transcripts are complete regardless.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
