"""Probe: chunk_ms sweep — does MiniCPM-o stay coherent at smaller audio chunks?

MiniCPM-o was trained at chunk_ms=1000. The turn-free design targets ~200 ms
chunks for finer barge-in granularity (design §8, exec-plan PR5a). This probe
sweeps chunk_ms in {1000, 500, 250, 200, 100} and, for each, runs a fixed prompt
and measures:
  - speech coherence (full text + heuristics: word count, unique ratio, max
    single-word repetition)
  - is_listen pattern (speak vs listen chunk counts; does it transition sanely)
  - per-chunk latency (mean cost_llm, cost_all from streaming_generate)

A fresh duplex wrapper is built per chunk_ms via base.as_duplex(chunk_ms=...)
sharing the one loaded base model (no reload). Each iteration is independent
(prepare() resets state). Failures to construct/run at a given chunk_ms are
recorded (a NO-GO for that size is itself a finding).

Usage:
    CUDA_VISIBLE_DEVICES=1 /raid/yid042/venvs/companion-harness/bin/python \
        scripts/probe_chunk_ms_sweep.py

Outputs:
    /tmp/probe-chunk-ms-sweep.json
    /tmp/probe-chunk-ms-sweep-summary.txt
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import numpy as np

try:
    from scripts.probe_turn_gate_barge_in import _synthesize, _SAMPLE_RATE
except ImportError:
    from probe_turn_gate_barge_in import _synthesize, _SAMPLE_RATE

_PROMPT = "Tell me about the moon and what a traveler would see there, in a few sentences."
_CHUNK_MS_SWEEP = [1000, 500, 250, 200, 100]
_MAX_SILENCE_CHUNKS = 25  # cap per chunk_ms (in chunk units, scales with chunk_ms)


def _coherence(text: str) -> dict:
    words = [w for w in text.split() if w.strip()]
    n = len(words)
    uniq = len(set(w.lower() for w in words))
    rep = (max(Counter(w.lower() for w in words).values()) / n) if n else 0.0
    alpha_chars = sum(c.isalpha() for c in text)
    return {
        "chars": len(text),
        "n_words": n,
        "unique_ratio": round(uniq / n, 3) if n else 0.0,
        "max_word_repetition_ratio": round(rep, 3),
        "alpha_fraction": round(alpha_chars / len(text), 3) if text else 0.0,
        "looks_coherent": n >= 3 and rep < 0.5 and (alpha_chars / len(text) if text else 0) > 0.5,
    }


def run_chunk_ms(base, chunk_ms: int) -> dict:
    print(f"\n=== chunk_ms={chunk_ms} ===", flush=True)
    try:
        duplex = base.as_duplex(generate_audio=False, chunk_ms=chunk_ms,
                                first_chunk_ms=chunk_ms + 35)
    except Exception as exc:
        print(f"  as_duplex failed: {type(exc).__name__}: {exc}", flush=True)
        return {"chunk_ms": chunk_ms, "construct_error": f"{type(exc).__name__}: {exc}"}

    from companion_harness.foreground_model_minicpm import _DEFAULT_DUPLEX_SYSTEM_PROMPT
    try:
        duplex.prepare(prefix_system_prompt=_DEFAULT_DUPLEX_SYSTEM_PROMPT)
    except Exception as exc:
        print(f"  prepare failed: {type(exc).__name__}: {exc}", flush=True)
        return {"chunk_ms": chunk_ms, "prepare_error": f"{type(exc).__name__}: {exc}"}

    chunk_samples = int(chunk_ms / 1000 * _SAMPLE_RATE)
    prompt_audio = _synthesize(_PROMPT)
    silence = np.zeros(chunk_samples, dtype=np.float32)

    buf = prompt_audio.copy()
    text_parts: list[str] = []
    speak_chunks = 0
    listen_chunks = 0
    cost_llm: list[float] = []
    cost_all: list[float] = []
    consec_listen = 0
    spoke = False
    n = 0
    phase = "prompt"

    def _gen(chunk):
        duplex.streaming_prefill(audio_waveform=chunk)
        return duplex.streaming_generate(
            max_new_speak_tokens_per_chunk=duplex.max_new_speak_tokens_per_chunk,
            temperature=duplex.temperature, top_k=duplex.top_k, top_p=duplex.top_p,
            listen_prob_scale=duplex.listen_prob_scale,
            text_repetition_penalty=duplex.text_repetition_penalty,
            text_repetition_window_size=duplex.text_repetition_window_size,
        )

    while n < (len(prompt_audio) // chunk_samples + _MAX_SILENCE_CHUNKS + 2):
        n += 1
        if phase == "prompt":
            if len(buf) >= chunk_samples:
                chunk, buf = buf[:chunk_samples], buf[chunk_samples:]
            elif len(buf) > 0:
                pad = np.zeros(chunk_samples - len(buf), dtype=np.float32)
                chunk = np.concatenate([buf, pad]); buf = np.array([], np.float32)
            else:
                phase = "silence"; chunk = silence
        else:
            chunk = silence
        try:
            r = _gen(chunk)
        except Exception as exc:
            print(f"  chunk {n} gen error: {type(exc).__name__}: {exc}", flush=True)
            return {"chunk_ms": chunk_ms, "gen_error": f"{type(exc).__name__}: {exc}",
                    "partial_text": " ".join(text_parts)}
        is_listen = bool(r.get("is_listen", True))
        txt = r.get("text", "") or ""
        if txt:
            text_parts.append(txt)
        if is_listen:
            listen_chunks += 1; consec_listen += 1
        else:
            speak_chunks += 1; consec_listen = 0; spoke = True
        cost_llm.append(float(r.get("cost_llm", 0.0)))
        cost_all.append(float(r.get("cost_all", 0.0)))
        # stop once it has spoken and returned to sustained listen
        if spoke and phase == "silence" and consec_listen >= 3:
            break

    full = " ".join(text_parts).strip()
    print(f"  speak={speak_chunks} listen={listen_chunks} "
          f"mean_cost_llm={np.mean(cost_llm)*1000:.0f}ms text={full[:70]!r}", flush=True)
    return {
        "chunk_ms": chunk_ms,
        "chunk_samples": chunk_samples,
        "speak_chunks": speak_chunks,
        "listen_chunks": listen_chunks,
        "spoke": spoke,
        "mean_cost_llm_ms": round(float(np.mean(cost_llm)) * 1000, 1) if cost_llm else None,
        "mean_cost_all_ms": round(float(np.mean(cost_all)) * 1000, 1) if cost_all else None,
        "full_text": full,
        "coherence": _coherence(full),
    }


def main() -> int:
    import torch
    if not torch.cuda.is_available():
        print("ERROR: no CUDA", flush=True); return 1
    free_gb = torch.cuda.mem_get_info()[0] / 1e9
    print(f"CUDA free VRAM: {free_gb:.1f} GB", flush=True)
    if free_gb < 10.0:
        print("GPU busy — set CUDA_VISIBLE_DEVICES.", flush=True); return 1

    print("Loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    model = MiniCPMStreamingModel()
    base = model._base
    print(f"  loaded in {round((time.monotonic()-t0)*1000)} ms", flush=True)

    results = []
    for cm in _CHUNK_MS_SWEEP:
        try:
            results.append(run_chunk_ms(base, cm))
        except Exception as exc:
            results.append({"chunk_ms": cm, "fatal": f"{type(exc).__name__}: {exc}"})
            print(f"  fatal at chunk_ms={cm}: {exc}", flush=True)

    Path("/tmp/probe-chunk-ms-sweep.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    lines = ["probe_chunk_ms_sweep summary", "",
             f"{'chunk_ms':>8} {'speak':>5} {'listen':>6} {'cost_llm':>9} {'coherent':>8} {'uniq':>5} {'rep':>5}  text",
             "-" * 110]
    baseline_coherent = None
    for r in results:
        cm = r["chunk_ms"]
        if any(k in r for k in ("construct_error", "prepare_error", "gen_error", "fatal")):
            err = r.get("construct_error") or r.get("prepare_error") or r.get("gen_error") or r.get("fatal")
            lines.append(f"{cm:>8}  FAILED: {err}")
            continue
        c = r["coherence"]
        if cm == 1000:
            baseline_coherent = c["looks_coherent"]
        lines.append(
            f"{cm:>8} {r['speak_chunks']:>5} {r['listen_chunks']:>6} "
            f"{str(r['mean_cost_llm_ms'])+'ms':>9} {str(c['looks_coherent']):>8} "
            f"{c['unique_ratio']:>5} {c['max_word_repetition_ratio']:>5}  {r['full_text'][:60]!r}")

    # verdict: smallest chunk_ms that stays coherent
    ok = [r["chunk_ms"] for r in results
          if "coherence" in r and r["coherence"]["looks_coherent"] and r.get("spoke")]
    lines += ["", "VERDICT:"]
    if ok:
        smallest = min(ok)
        lines.append(f"  Smallest coherent chunk_ms = {smallest}.")
        if smallest <= 200:
            lines.append("  => ~200ms target VIABLE (model stays coherent). PR5a can target it; tune listen calibration.")
        elif smallest <= 500:
            lines.append(f"  => 200ms degrades; safe floor ~{smallest}ms. PR5a target should be {smallest}ms, not 200ms.")
        else:
            lines.append("  => Only near-1000ms stays coherent; smaller chunks degrade. ~200ms target NOT free — needs finetune or stays ~1000ms.")
    else:
        lines.append("  No chunk_ms produced coherent speech — check probe wiring / model state.")

    summary = "\n".join(lines)
    print("\n" + summary, flush=True)
    Path("/tmp/probe-chunk-ms-sweep-summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n=> /tmp/probe-chunk-ms-sweep.json + summary", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
