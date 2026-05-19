"""Probe: torch.compile validation + max_new_speak_tokens_per_chunk sweep.

For each (enable_torch_compile, max_new_speak_tokens_per_chunk) cell:
  - Loads MiniCPMStreamingModel with the given compile flag
  - Optionally warms up (cold-vs-warm latency)
  - Runs 3 sequential turns of Kokoro audio (no reset between turns)
  - Records per-chunk latency stats + cross-turn pass/fail

Outputs:
    /tmp/probe-sweep-results.json       — structured per-cell data
    /tmp/probe-sweep-summary.txt        — human-readable grid + verdict

Run:
    /raid/yid042/venvs/companion-harness/bin/python scripts/probe_pathb_compile_chunksize_sweep.py
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = _SAMPLE_RATE  # 1-second chunks

_KOKORO_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_VOICES = "/raid/yid042/models/kokoro/voices.json"

_TURNS = [
    "Hello, how are you?",
    "Tell me about cats.",
    "What about dogs?",
]

_CAP_VALUES = [5, 10, 20, 30, 50]
_LISTEN_CONFIRM = 3
_MAX_SILENCE_CHUNKS = 30


# ---------------------------------------------------------------------------
# Audio helpers (mirrors probe_pathb_cross_turn_mic)
# ---------------------------------------------------------------------------

def _resample_nearest(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return audio
    out_len = int(len(audio) * dst_rate / src_rate)
    indices = np.round(np.arange(out_len) * src_rate / dst_rate).astype(int)
    indices = np.clip(indices, 0, len(audio) - 1)
    return audio[indices]


def _synthesize_kokoro(phrase: str) -> np.ndarray:
    import asyncio
    if not Path(_KOKORO_MODEL).exists() or not Path(_KOKORO_VOICES).exists():
        print(f"  warn: Kokoro not found — using 1s silence for {phrase!r}", flush=True)
        return np.zeros(_SAMPLE_RATE, dtype=np.float32)
    from companion_harness.tts_kokoro import KokoroTtsAdapter
    adapter = KokoroTtsAdapter(model_path=_KOKORO_MODEL, voices_path=_KOKORO_VOICES, warmup=False)

    async def _collect() -> list[bytes]:
        chunks = []
        async for chunk in adapter.synthesize(phrase, []):
            chunks.append(chunk)
        return chunks

    raw = asyncio.get_event_loop().run_until_complete(_collect())
    if not raw:
        return np.zeros(_SAMPLE_RATE, dtype=np.float32)
    pcm16 = np.frombuffer(b"".join(raw), dtype="<i2").astype(np.float32) / 32768.0
    return _resample_nearest(pcm16, 24000, _SAMPLE_RATE)


# ---------------------------------------------------------------------------
# Chunk-level inference (direct duplex calls, cap injected per cell)
# ---------------------------------------------------------------------------

def _process_chunk(duplex, audio: np.ndarray, cap: int) -> tuple[dict, float]:
    """Returns (result_dict, wall_time_ms)."""
    t0 = time.monotonic()
    duplex.streaming_prefill(audio_waveform=audio)
    result = duplex.streaming_generate(
        max_new_speak_tokens_per_chunk=cap,
        temperature=duplex.temperature,
        top_k=duplex.top_k,
        top_p=duplex.top_p,
        listen_prob_scale=duplex.listen_prob_scale,
        text_repetition_penalty=duplex.text_repetition_penalty,
        text_repetition_window_size=duplex.text_repetition_window_size,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    return result, elapsed_ms


# ---------------------------------------------------------------------------
# Single-turn runner (no reset between turns)
# ---------------------------------------------------------------------------

def run_turn(duplex, input_audio: np.ndarray, cap: int, turn_idx: int,
             turn_text: str) -> tuple[dict, list[float]]:
    silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    n_input_chunks = int(np.ceil(len(input_audio) / _CHUNK_SAMPLES))

    text_parts: list[str] = []
    chunk_latencies_ms: list[float] = []
    consecutive_listen = 0
    first_speak_chunk: int | None = None
    self_terminated = False
    hit_cap = False
    buf = input_audio.copy()
    phase = "input"
    chunk_idx = 0

    while True:
        chunk_idx += 1
        if phase == "input" and len(buf) > 0:
            if len(buf) >= _CHUNK_SAMPLES:
                chunk, buf = buf[:_CHUNK_SAMPLES], buf[_CHUNK_SAMPLES:]
            else:
                pad = np.zeros(_CHUNK_SAMPLES - len(buf), dtype=np.float32)
                chunk = np.concatenate([buf, pad])
                buf = np.array([], dtype=np.float32)
        else:
            phase = "silence"
            chunk = silence

        try:
            result, latency_ms = _process_chunk(duplex, chunk, cap)
        except Exception as exc:
            print(f"    chunk {chunk_idx}: ERROR {exc}", flush=True)
            break

        chunk_latencies_ms.append(latency_ms)

        text = result.get("text", "") or ""
        is_listen = bool(result.get("is_listen", True))

        if text:
            text_parts.append(text)

        if not is_listen and first_speak_chunk is None:
            first_speak_chunk = chunk_idx

        if is_listen:
            consecutive_listen += 1
        else:
            consecutive_listen = 0

        print(f"    chunk {chunk_idx} [{phase}] listen={is_listen} "
              f"consec={consecutive_listen} {latency_ms:.0f}ms {text[:40]!r}", flush=True)

        if consecutive_listen >= _LISTEN_CONFIRM and first_speak_chunk is not None:
            self_terminated = True
            break

        if phase == "silence" and chunk_idx >= n_input_chunks + _MAX_SILENCE_CHUNKS:
            hit_cap = True
            break

    response_text = " ".join(text_parts).strip()
    assistant_started = first_speak_chunk is not None
    pass_fail = "PASS" if (assistant_started and self_terminated) else "FAIL"

    print(f"  => Turn {turn_idx} ({turn_text!r}): {pass_fail}  "
          f"chars={len(response_text)}  text={response_text[:60]!r}", flush=True)

    turn_data = {
        "turn_idx": turn_idx,
        "prompt": turn_text,
        "pass_fail": pass_fail,
        "response_text": response_text,
        "response_chars": len(response_text),
        "response_preview": response_text[:60],
        "assistant_started": assistant_started,
        "self_terminated": self_terminated,
        "hit_cap": hit_cap,
    }
    return turn_data, chunk_latencies_ms


# ---------------------------------------------------------------------------
# Warmup measurement (compile cold-vs-warm latency)
# ---------------------------------------------------------------------------

def _warmup_compile(duplex, base_audios: list[np.ndarray], cap: int) -> tuple[float, float]:
    """Returns (cold_ms, warm_ms): first-chunk vs second-chunk latency."""
    # Use silence chunk for warmup so we don't consume base_audios state
    silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    # First call = cold (triggers triton kernel compilation)
    _, cold_ms = _process_chunk(duplex, silence, cap)
    # Second call = warm
    _, warm_ms = _process_chunk(duplex, silence, cap)
    print(f"  compile warmup: cold={cold_ms:.0f}ms  warm={warm_ms:.0f}ms", flush=True)
    return cold_ms, warm_ms


# ---------------------------------------------------------------------------
# Per-cell runner
# ---------------------------------------------------------------------------

def run_cell(model, base_audios: list[np.ndarray], compile_flag: bool, cap: int,
             system_prompt: str) -> dict:
    duplex = model._duplex

    # Reset session state before each cell to get a clean starting point
    try:
        model.reset_streaming_session(caused_by=["sweep_probe"])
    except Exception as exc:
        print(f"  warn: reset_streaming_session raised {type(exc).__name__}: {exc}", flush=True)

    # Prepare duplex for this cell's 3 turns
    duplex.prepare(prefix_system_prompt=system_prompt)

    compile_status = "n/a"
    compile_warmup_ms = -1.0
    compile_cold_ms = -1.0

    if compile_flag:
        compile_status = "PASS" if model._torch_compile_active else "FAIL"
        if model._torch_compile_active:
            print(f"  torch.compile active — measuring cold/warm latency...", flush=True)
            try:
                compile_cold_ms, compile_warmup_ms = _warmup_compile(duplex, base_audios, cap)
                # After warmup used silence chunks, re-prepare to reset KV state
                duplex.prepare(prefix_system_prompt=system_prompt)
            except Exception as exc:
                print(f"  compile warmup failed: {exc}", flush=True)
                compile_status = "FAIL"

    # 3 sequential turns (no reset between turns — matches Path B continuous flow)
    all_latencies_ms: list[float] = []
    turn_results: list[dict] = []
    for i, (phrase, audio) in enumerate(zip(_TURNS, base_audios), start=1):
        print(f"\n  Turn {i}: {phrase!r}", flush=True)
        turn_data, chunk_ms = run_turn(duplex, audio, cap, i, phrase)
        turn_results.append(turn_data)
        all_latencies_ms.extend(chunk_ms)

    # Aggregate latency stats
    if all_latencies_ms:
        sorted_ms = sorted(all_latencies_ms)
        p25_ms = sorted_ms[len(sorted_ms) // 4]
        p50_ms = statistics.median(all_latencies_ms)
        p75_ms = sorted_ms[min(3 * len(sorted_ms) // 4, len(sorted_ms) - 1)]
    else:
        p25_ms = p50_ms = p75_ms = 0.0

    cross_turn_pass = all(t["pass_fail"] == "PASS" for t in turn_results)
    turn_chars = [t["response_chars"] for t in turn_results]
    rtf_at_p50 = p50_ms / 1000.0  # chunks are 1s of audio

    return {
        "compile": compile_flag,
        "cap": cap,
        "compile_status": compile_status,
        "compile_cold_ms": round(compile_cold_ms),
        "compile_warmup_ms": round(compile_warmup_ms),
        "per_chunk_median_ms": round(p50_ms),
        "per_chunk_p25_ms": round(p25_ms),
        "per_chunk_p75_ms": round(p75_ms),
        "rtf_at_p50": round(rtf_at_p50, 3),
        "cross_turn_pass": cross_turn_pass,
        "turn_response_chars": turn_chars,
        "turn_response_previews": [t["response_preview"] for t in turn_results],
        "turn_results": turn_results,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("probe_pathb_compile_chunksize_sweep: loading model...", flush=True)

    import torch
    if not torch.cuda.is_available():
        print("ERROR: no CUDA device", flush=True)
        return 1
    free_gb = torch.cuda.mem_get_info()[0] / 1e9
    print(f"  CUDA free VRAM: {free_gb:.1f} GB", flush=True)
    if free_gb < 10.0:
        print("GPU busy (free VRAM < 10 GB) — stop console first.", flush=True)
        return 1

    from companion_harness.foreground_model_minicpm import (
        MiniCPMStreamingModel,
        _DEFAULT_DUPLEX_SYSTEM_PROMPT,
    )

    # We need two model instances: one without compile, one with compile.
    # Load uncompiled model first.
    print("\n--- Loading uncompiled model ---", flush=True)
    t0 = time.monotonic()
    model_eager = MiniCPMStreamingModel(enable_torch_compile=False)
    print(f"  loaded in {round((time.monotonic() - t0) * 1000)} ms", flush=True)

    print("\nSynthesizing Kokoro audio for all 3 turns...", flush=True)
    base_audios = [_synthesize_kokoro(p) for p in _TURNS]
    for i, (p, a) in enumerate(zip(_TURNS, base_audios), start=1):
        rms = float(np.sqrt(np.mean(a ** 2)))
        print(f"  Turn {i} ({p!r}): {len(a)/16000:.2f}s  RMS={rms:.4f}", flush=True)

    all_cells: list[dict] = []

    # --- Eager (uncompiled) cells ---
    for cap in _CAP_VALUES:
        print(f"\n{'='*70}", flush=True)
        print(f"CELL: compile=False  cap={cap}", flush=True)
        print(f"{'='*70}", flush=True)
        cell = run_cell(model_eager, base_audios, False, cap, _DEFAULT_DUPLEX_SYSTEM_PROMPT)
        all_cells.append(cell)
        print(f"  => p50={cell['per_chunk_median_ms']}ms  RTF={cell['rtf_at_p50']:.3f}  "
              f"cross_turn={'PASS' if cell['cross_turn_pass'] else 'FAIL'}", flush=True)

    # Free the eager model before loading the compiled one
    del model_eager
    torch.cuda.empty_cache()

    # --- Compiled cells ---
    print("\n--- Loading compiled model (torch.compile=True) ---", flush=True)
    t0 = time.monotonic()
    try:
        model_compiled = MiniCPMStreamingModel(enable_torch_compile=True)
        print(f"  loaded in {round((time.monotonic() - t0) * 1000)} ms", flush=True)
        print(f"  _torch_compile_active={model_compiled._torch_compile_active}", flush=True)
    except Exception as exc:
        print(f"  compile model load FAILED: {type(exc).__name__}: {exc}", flush=True)
        for cap in _CAP_VALUES:
            all_cells.append({
                "compile": True, "cap": cap,
                "compile_status": "FAIL",
                "compile_cold_ms": -1, "compile_warmup_ms": -1,
                "per_chunk_median_ms": -1, "per_chunk_p25_ms": -1, "per_chunk_p75_ms": -1,
                "rtf_at_p50": -1.0, "cross_turn_pass": False,
                "turn_response_chars": [-1, -1, -1],
                "turn_response_previews": ["", "", ""],
                "turn_results": [],
                "load_error": str(exc),
            })
        model_compiled = None

    if model_compiled is not None:
        for cap in _CAP_VALUES:
            print(f"\n{'='*70}", flush=True)
            print(f"CELL: compile=True  cap={cap}", flush=True)
            print(f"{'='*70}", flush=True)
            cell = run_cell(model_compiled, base_audios, True, cap, _DEFAULT_DUPLEX_SYSTEM_PROMPT)
            all_cells.append(cell)
            print(f"  => p50={cell['per_chunk_median_ms']}ms  RTF={cell['rtf_at_p50']:.3f}  "
                  f"cross_turn={'PASS' if cell['cross_turn_pass'] else 'FAIL'}  "
                  f"compile_status={cell['compile_status']}", flush=True)
        del model_compiled
        torch.cuda.empty_cache()

    # --- Save JSON ---
    json_path = "/tmp/probe-sweep-results.json"
    Path(json_path).write_text(json.dumps(all_cells, indent=2), encoding="utf-8")
    print(f"\n=> saved {json_path}", flush=True)

    # --- Summary table ---
    header = (
        f"{'compile':<8} {'chunk_cap':>9} {'compile_ok':>10} {'warm_ms':>7} "
        f"{'p50ms':>6} {'RTF':>6} {'T1chars':>7} {'T2chars':>7} {'T3chars':>7} {'all_pass':>8}"
    )
    sep = "-" * 90
    lines = [
        "probe_pathb_compile_chunksize_sweep — torch.compile + chunk cap sweep",
        "",
        header,
        sep,
    ]

    for cell in all_cells:
        c = "True" if cell["compile"] else "False"
        cap = cell["cap"]
        cok = cell["compile_status"]
        warm = f"{cell['compile_warmup_ms']}" if cell["compile_warmup_ms"] >= 0 else "-"
        p50 = f"{cell['per_chunk_median_ms']}" if cell["per_chunk_median_ms"] >= 0 else "-"
        rtf = f"{cell['rtf_at_p50']:.3f}" if cell["rtf_at_p50"] >= 0 else "-"
        chars = cell["turn_response_chars"]
        t1 = str(chars[0]) if len(chars) > 0 else "-"
        t2 = str(chars[1]) if len(chars) > 1 else "-"
        t3 = str(chars[2]) if len(chars) > 2 else "-"
        ap = "yes" if cell["cross_turn_pass"] else "no"
        lines.append(
            f"{c:<8} {cap:>9} {cok:>10} {warm:>7} {p50:>6} {rtf:>6} {t1:>7} {t2:>7} {t3:>7} {ap:>8}"
        )

    lines += ["", sep, ""]

    # --- Verdict ---
    # Find passing cells: compile PASS (or n/a), RTF < 0.8, cross_turn, chars > 30 per turn
    passing = [
        c for c in all_cells
        if c["compile_status"] in ("PASS", "n/a")
        and 0 <= c["rtf_at_p50"] < 0.8
        and c["cross_turn_pass"]
        and all(ch > 30 for ch in c["turn_response_chars"])
    ]

    if passing:
        # Prefer compiled cells; then lowest RTF
        compiled_passing = [c for c in passing if c["compile"]]
        best = min(compiled_passing or passing, key=lambda c: c["rtf_at_p50"])
        avg_chars = sum(best["turn_response_chars"]) / 3
        lines.append(
            f"RECOMMENDED: enable_torch_compile={best['compile']}, "
            f"max_new_speak_tokens_per_chunk={best['cap']}"
        )
        lines.append(
            f"- Per-chunk p50: {best['per_chunk_median_ms']}ms "
            f"(RTF: {best['rtf_at_p50']:.3f}x)"
        )
        lines.append("- Cross-turn: all 3 PASS")
        lines.append(f"- Response density: avg {avg_chars:.0f} chars per turn")
    else:
        lines.append(
            "VERDICT: NO CELL SATISFIES ALL CRITERIA "
            "(compile=PASS + RTF<0.8 + cross_turn + >30 chars/turn)."
        )
        # Find best achievable
        rtf_ok = [c for c in all_cells if 0 <= c["rtf_at_p50"] < 0.8]
        if rtf_ok:
            lines.append(
                f"  RTF<0.8 achievable: {len(rtf_ok)} cells pass RTF gate but "
                "fail quality or compile."
            )
        else:
            lines.append(
                "  Path B not achievable on b200 with current model. "
                "Need [model swap | vLLM | bigger optimization]."
            )

    lines.append("")

    # Response previews
    lines.append("--- Response previews ---")
    for cell in all_cells:
        c = "compile" if cell["compile"] else "eager"
        lines.append(f"  {c}/cap={cell['cap']}:")
        for i, preview in enumerate(cell.get("turn_response_previews", []), start=1):
            lines.append(f"    T{i}: {preview!r}")

    lines.append("")

    summary = "\n".join(lines)
    print("\n" + summary, flush=True)
    summary_path = "/tmp/probe-sweep-summary.txt"
    Path(summary_path).write_text(summary + "\n", encoding="utf-8")
    print(f"=> saved {summary_path}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
