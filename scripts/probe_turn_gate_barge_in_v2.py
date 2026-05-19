"""Probe v2: controlled barge-in causality for MiniCPM-o turn-gate.

v1 (scripts/probe_turn_gate_barge_in.py) showed that with the mid-turn listen
gate bypassed the model yields during a barge-in and responds coherently, but
it was a single non-deterministic trial and the model's turn sometimes ended
before the barge-in landed. v2 makes it controlled:

  - Gate bypassed throughout (current_turn_ended always True).
  - listen_prob_scale set LOW (bias the model to KEEP speaking) so it reliably
    monologues — guaranteeing the test audio lands mid-utterance.
  - Two conditions, N trials each:
       SILENCE : once the model is speaking, feed silence; measure chunks-to-yield
       BARGE   : once the model is speaking, feed a barge-in utterance; measure same
  - If BARGE yields earlier than SILENCE despite the speak-bias, the barge-in
    audio is causally driving the yield (not just elapsed time / natural turn end).

Usage:
    CUDA_VISIBLE_DEVICES=1 /raid/yid042/venvs/companion-harness/bin/python \
        scripts/probe_turn_gate_barge_in_v2.py [--trials 3] [--listen-prob-scale 0.3]

Outputs:
    /tmp/probe-turn-gate-v2.json
    /tmp/probe-turn-gate-v2-summary.txt
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import numpy as np

try:  # reuse v1 helpers — works under both `python -m scripts.x` and `python scripts/x.py`
    from scripts.probe_turn_gate_barge_in import (
        _AlwaysEnded, _process_chunk, _synthesize,
        _CHUNK_SAMPLES, _SAMPLE_RATE, _PROMPT_TEXT, _BARGE_IN_TEXT,
    )
except ImportError:
    from probe_turn_gate_barge_in import (
        _AlwaysEnded, _process_chunk, _synthesize,
        _CHUNK_SAMPLES, _SAMPLE_RATE, _PROMPT_TEXT, _BARGE_IN_TEXT,
    )

_MAX_AWAIT_SPEAK = 12     # chunks to wait for the model to start speaking
_MAX_OBSERVE = 16         # chunks of condition audio to observe for a yield
_YIELD_CONFIRM = 2        # consecutive is_listen=True chunks = a confirmed yield


def _run_condition(duplex, system_prompt, prompt_audio, condition_audio,
                   label) -> dict:
    duplex.prepare(prefix_system_prompt=system_prompt)
    silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    prompt_buf = prompt_audio.copy()
    cond_buf = condition_audio.copy()

    phase = "prompt"
    chunk_idx = 0
    await_count = 0
    observe_count = 0
    speaking_seen = False
    speak_start = None
    cond_start = None
    consec_listen = 0
    yield_chunk = None
    speak_text: list[str] = []
    cond_text: list[str] = []

    while True:
        chunk_idx += 1
        if phase == "prompt":
            if len(prompt_buf) >= _CHUNK_SAMPLES:
                chunk, prompt_buf = prompt_buf[:_CHUNK_SAMPLES], prompt_buf[_CHUNK_SAMPLES:]
            elif len(prompt_buf) > 0:
                pad = np.zeros(_CHUNK_SAMPLES - len(prompt_buf), dtype=np.float32)
                chunk = np.concatenate([prompt_buf, pad]); prompt_buf = np.array([], np.float32)
            else:
                phase = "await"; chunk = silence
            kind = "prompt" if phase == "prompt" else "await"
        elif phase == "await":
            chunk = silence; kind = "await"
        elif phase == "condition":
            if len(cond_buf) >= _CHUNK_SAMPLES:
                chunk, cond_buf = cond_buf[:_CHUNK_SAMPLES], cond_buf[_CHUNK_SAMPLES:]
            elif len(cond_buf) > 0:
                pad = np.zeros(_CHUNK_SAMPLES - len(cond_buf), dtype=np.float32)
                chunk = np.concatenate([cond_buf, pad]); cond_buf = np.array([], np.float32)
            else:
                chunk = silence  # condition audio exhausted -> silence tail, stay in phase
            kind = "condition"
        else:
            break

        try:
            result = _process_chunk(duplex, chunk)
        except Exception as exc:
            print(f"    ch{chunk_idx}: ERROR {type(exc).__name__}: {exc}", flush=True)
            break

        is_listen = bool(result.get("is_listen", True))
        text = result.get("text", "") or ""
        if not is_listen:
            speaking_seen = True
            if speak_start is None:
                speak_start = chunk_idx
        consec_listen = consec_listen + 1 if is_listen else 0

        if kind in ("prompt", "await") and text:
            speak_text.append(text)
        if kind == "condition":
            if text:
                cond_text.append(text)
            if consec_listen >= _YIELD_CONFIRM and yield_chunk is None:
                yield_chunk = chunk_idx - _YIELD_CONFIRM + 1  # first listen of the run

        print(f"    ch{chunk_idx:>2} [{kind:<9}] listen={int(is_listen)} "
              f"consec={consec_listen} text={text[:34]!r}", flush=True)

        if phase == "await":
            await_count += 1
            if speaking_seen:
                phase = "condition"; cond_start = chunk_idx + 1; observe_count = 0
            elif await_count >= _MAX_AWAIT_SPEAK:
                break
        elif phase == "condition":
            observe_count += 1
            if yield_chunk is not None:
                # keep one extra chunk for context then stop
                break
            if observe_count >= _MAX_OBSERVE:
                break

    chunks_to_yield = (yield_chunk - cond_start) if (yield_chunk is not None and cond_start is not None) else None
    return {
        "label": label,
        "speak_start": speak_start,
        "cond_start": cond_start,
        "yield_chunk": yield_chunk,
        "chunks_to_yield": chunks_to_yield,        # None = never yielded within observe window
        "yielded": yield_chunk is not None,
        "speak_text": " ".join(speak_text).strip(),
        "cond_text": " ".join(cond_text).strip(),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--listen-prob-scale", type=float, default=0.3)
    args = ap.parse_args()

    import torch
    if not torch.cuda.is_available():
        print("ERROR: no CUDA", flush=True); return 1
    free_gb = torch.cuda.mem_get_info()[0] / 1e9
    print(f"CUDA free VRAM: {free_gb:.1f} GB", flush=True)
    if free_gb < 10.0:
        print("GPU busy — set CUDA_VISIBLE_DEVICES to a free device.", flush=True); return 1

    print("Loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import (
        MiniCPMStreamingModel, _DEFAULT_DUPLEX_SYSTEM_PROMPT,
    )
    model = MiniCPMStreamingModel()
    duplex = model._duplex
    print(f"  loaded in {round((time.monotonic()-t0)*1000)} ms", flush=True)

    # bias toward speaking + open the gate for the whole experiment
    duplex.listen_prob_scale = args.listen_prob_scale
    type(duplex).current_turn_ended = _AlwaysEnded()
    print(f"  listen_prob_scale={args.listen_prob_scale}  gate=BYPASSED", flush=True)

    print("Synthesizing prompt + barge-in...", flush=True)
    prompt_audio = _synthesize(_PROMPT_TEXT)
    barge_audio = _synthesize(_BARGE_IN_TEXT)
    silence_audio = np.zeros(len(barge_audio), dtype=np.float32)  # same length as barge

    results = {"silence": [], "barge": []}
    try:
        for t in range(args.trials):
            print(f"\n=== trial {t+1}/{args.trials} — SILENCE ===", flush=True)
            results["silence"].append(
                _run_condition(duplex, _DEFAULT_DUPLEX_SYSTEM_PROMPT, prompt_audio, silence_audio, f"silence-{t}"))
            print(f"\n=== trial {t+1}/{args.trials} — BARGE ===", flush=True)
            results["barge"].append(
                _run_condition(duplex, _DEFAULT_DUPLEX_SYSTEM_PROMPT, prompt_audio, barge_audio, f"barge-{t}"))
    finally:
        del type(duplex).current_turn_ended

    def _summ(rows):
        ys = [r["chunks_to_yield"] for r in rows if r["chunks_to_yield"] is not None]
        n_yield = sum(1 for r in rows if r["yielded"])
        med = sorted(ys)[len(ys)//2] if ys else None
        return n_yield, len(rows), med, ys

    s_n, s_tot, s_med, s_ys = _summ(results["silence"])
    b_n, b_tot, b_med, b_ys = _summ(results["barge"])

    lines = [
        "probe_turn_gate_barge_in_v2 summary (controlled)",
        f"trials={args.trials}  listen_prob_scale={args.listen_prob_scale}  gate=BYPASSED",
        "",
        f"SILENCE: yielded {s_n}/{s_tot}  chunks_to_yield={s_ys}  median={s_med}",
        f"BARGE  : yielded {b_n}/{b_tot}  chunks_to_yield={b_ys}  median={b_med}",
        "",
        "sample speak text (barge trial 0): " + (results["barge"][0]["speak_text"][:90] if results["barge"] else ""),
        "sample post-barge text (barge trial 0): " + (results["barge"][0]["cond_text"][:90] if results["barge"] else ""),
        "",
        "VERDICT:",
    ]
    if b_n > s_n or (b_med is not None and (s_med is None or b_med < s_med)):
        lines.append("  CAUSAL — barge-in yields more/earlier than silence despite speak-bias.")
        lines.append("  Model-judged barge-in is real; turn-free is an ORCHESTRATION problem. GO.")
    elif b_n == 0 and s_n == 0:
        lines.append("  NO YIELD in either condition — listen_prob_scale too low; re-run higher.")
    else:
        lines.append("  WEAK/NULL — no clear barge>silence effect; inspect per-trial JSON.")

    summary = "\n".join(lines)
    print("\n" + summary, flush=True)
    Path("/tmp/probe-turn-gate-v2.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    Path("/tmp/probe-turn-gate-v2-summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n=> /tmp/probe-turn-gate-v2.json + summary", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
