"""Probe (PR1 precondition): audio_past_key_values mid-generation reset coherence.

The audio encoder KV (`audio_past_key_values`) caps (~1500 tokens) and auto-resets
(modeling_minicpmo.py:565). In the turn-free design there is no per-turn KV reset,
so long continuous sessions WILL hit this reset mid-stream. PR1 must know: does the
model stay coherent across the reset, or does context/coherence break?

Method: feed continuous synthesized audio in 1 s chunks; monitor the audio-KV length
each chunk; detect the reset (length drops); capture the model's spoken text across
the run; assess whether output stays coherent/on-topic after the reset boundary.

Usage:
    CUDA_VISIBLE_DEVICES=1 /raid/yid042/venvs/companion-harness/bin/python \
        scripts/probe_audio_kv_reset.py

Outputs:
    /tmp/probe-audio-kv-reset.json
    /tmp/probe-audio-kv-reset-summary.txt
"""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path

import numpy as np

try:
    from scripts.probe_turn_gate_barge_in import _synthesize, _process_chunk, _CHUNK_SAMPLES
except ImportError:
    from probe_turn_gate_barge_in import _synthesize, _process_chunk, _CHUNK_SAMPLES

# A long, multi-sentence continuous passage to accumulate audio tokens past the cap.
# Prompt that ELICITS a long model response, so the model is SPEAKING across the
# reset (the prior "please just listen" prompt made the model correctly stay
# silent → nothing to assess). listen_prob_scale is biased low in main() so the
# model keeps monologuing while silence chunks accumulate audio tokens to the cap.
_LONG_PROMPT = (
    "Please tell me a very long and detailed story about a traveler who explores many "
    "different countries and cities all around the world, describing each place, the food, "
    "the people, and the landscapes in vivid detail, and keep the story going as long as you can."
)


def _audio_kv_len(duplex) -> int | None:
    c = getattr(duplex.model, "audio_past_key_values", None)
    if c is None:
        return None
    try:
        if hasattr(c, "key_cache") and len(c.key_cache) > 0:
            return int(c.key_cache[0].shape[2])
        if isinstance(c, tuple) and len(c) > 0:
            return int(c[0][0].shape[2])
    except Exception:
        return -1
    return -1


def _coherence(text: str) -> dict:
    words = [w for w in text.split() if w.strip()]
    n = len(words)
    uniq = len(set(w.lower() for w in words))
    rep = (max(Counter(w.lower() for w in words).values()) / n) if n else 0.0
    return {"n_words": n, "unique_ratio": round(uniq / n, 3) if n else 0.0,
            "max_rep_ratio": round(rep, 3),
            "coherent": n >= 3 and rep < 0.5}


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
    from companion_harness.foreground_model_minicpm import (
        MiniCPMStreamingModel, _DEFAULT_DUPLEX_SYSTEM_PROMPT,
    )
    model = MiniCPMStreamingModel()
    duplex = model._duplex
    print(f"  loaded in {round((time.monotonic()-t0)*1000)} ms", flush=True)

    duplex.prepare(prefix_system_prompt=_DEFAULT_DUPLEX_SYSTEM_PROMPT)
    duplex.listen_prob_scale = 0.3  # bias toward speaking so the model monologues across the reset
    audio = _synthesize(_LONG_PROMPT)
    print(f"  prompt audio: {len(audio)/16000:.1f}s", flush=True)

    silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    buf = audio.copy()
    per_chunk: list[dict] = []
    prev_len = None
    reset_chunks: list[int] = []
    chunk_idx = 0
    max_chunks = 80  # cap

    while chunk_idx < max_chunks:
        chunk_idx += 1
        if len(buf) >= _CHUNK_SAMPLES:
            chunk, buf = buf[:_CHUNK_SAMPLES], buf[_CHUNK_SAMPLES:]
        elif len(buf) > 0:
            pad = np.zeros(_CHUNK_SAMPLES - len(buf), dtype=np.float32)
            chunk = np.concatenate([buf, pad]); buf = np.array([], np.float32)
        else:
            chunk = silence
        try:
            r = _process_chunk(duplex, chunk)
        except Exception as exc:
            print(f"  chunk {chunk_idx} ERROR {type(exc).__name__}: {exc}", flush=True)
            break
        kv_len = _audio_kv_len(duplex)
        # reset detected: length dropped vs previous (or went None) after having grown
        is_reset = (prev_len is not None and kv_len is not None and kv_len < prev_len - 50) or \
                   (prev_len is not None and prev_len > 100 and kv_len is None)
        if is_reset:
            reset_chunks.append(chunk_idx)
            print(f"  *** audio-KV RESET at chunk {chunk_idx} ({prev_len} -> {kv_len}) ***", flush=True)
        per_chunk.append({
            "chunk": chunk_idx, "audio_kv_len": kv_len,
            "is_listen": bool(r.get("is_listen", True)),
            "text": r.get("text", "") or "",
            "reset": is_reset,
        })
        prev_len = kv_len if kv_len is not None else prev_len
        # stop once we've seen a reset plus 10 more chunks (to observe post-reset coherence)
        if reset_chunks and chunk_idx >= reset_chunks[0] + 10:
            break
        # if input exhausted and no reset yet, keep feeding silence a bit then stop
        if len(buf) == 0 and chunk_idx > (len(audio)//_CHUNK_SAMPLES) + 15 and not reset_chunks:
            break

    # coherence before vs after the first reset
    out: dict = {"reset_chunks": reset_chunks, "total_chunks": chunk_idx,
                 "per_chunk": per_chunk}
    summary_lines = ["probe_audio_kv_reset summary", ""]
    if reset_chunks:
        rc = reset_chunks[0]
        before = " ".join(c["text"] for c in per_chunk if c["chunk"] < rc and c["text"]).strip()
        after = " ".join(c["text"] for c in per_chunk if c["chunk"] >= rc and c["text"]).strip()
        cb, ca = _coherence(before), _coherence(after)
        out["before_reset"] = {"text": before, "coherence": cb}
        out["after_reset"] = {"text": after, "coherence": ca}
        summary_lines += [
            f"audio-KV reset fired at chunk {rc} (of {chunk_idx}).",
            f"  before reset: coherent={cb['coherent']} uniq={cb['unique_ratio']} text={before[:80]!r}",
            f"  after  reset: coherent={ca['coherent']} uniq={ca['unique_ratio']} text={after[:80]!r}",
            "",
            "VERDICT:",
        ]
        if ca["coherent"]:
            summary_lines.append("  GO — model stays coherent across the audio-KV reset. PR1 can proceed; "
                                 "no special mitigation needed for the reset boundary (monitor in long sessions).")
        else:
            summary_lines.append("  NO-GO/CAUTION — output degrades after the reset. PR1 needs a mitigation "
                                 "(pre-reset boundary event / summary re-injection) before long-session operation.")
    else:
        summary_lines += [
            f"No audio-KV reset observed in {chunk_idx} chunks "
            f"(max audio_kv_len={max((c['audio_kv_len'] or 0) for c in per_chunk)}).",
            "Inconclusive — increase input length / max_chunks to force a reset.",
        ]

    summary = "\n".join(summary_lines)
    print("\n" + summary, flush=True)
    Path("/tmp/probe-audio-kv-reset.json").write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    Path("/tmp/probe-audio-kv-reset-summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n=> /tmp/probe-audio-kv-reset.json + summary", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
