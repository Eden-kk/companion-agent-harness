"""Probe: does chat(stream=True) fail due to low audio amplitude or mic content?

Stage 1 passes with Kokoro fixture (RMS ~0.15, 325 chars output).
Stage 4 mic-test fails with RMS 0.005-0.013 raw / 0.05-0.12 after peak-normalize.

This probe attenuates the same Kokoro fixture by 0.05x to simulate mic-level
audio, then tests three recovery strategies to isolate whether the failure is
an amplitude threshold in the model or a mic-content artefact.

Usage:
    python scripts/probe_minicpm_chat_stream_attenuation.py [--output /tmp/probe-attenuation.json]
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

import numpy as np


_SAMPLE_RATE = 16000
_KOKORO_MODEL   = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_VOICES  = "/raid/yid042/models/kokoro/voices.json"
_TEST_PHRASE = (
    "Hello, please tell me a short story about a curious cat exploring a "
    "quiet garden in the early morning. Take your time and describe what "
    "the cat sees, hears, and smells."
)

_DEFAULT_CHAT_STREAM_SYSTEM_PROMPT = (
    "You are a warm, conversational voice companion. The user just finished "
    "speaking a substantive turn. Reply with one or two natural, complete "
    "sentences (target 80–200 characters). Be specific. Answer questions "
    "directly first."
)
_DEFAULT_USER_INSTRUCTION = "Respond to the spoken turn above."
_CHAT_STREAM_MAX_NEW_TOKENS = 256
_CHAT_STREAM_TEMPERATURE = 0.6


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _resample_nearest(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio
    out_len = int(len(audio) * dst / src)
    idx = np.round(np.arange(out_len) * src / dst).astype(int)
    return audio[np.clip(idx, 0, len(audio) - 1)]


def _synthesize(phrase: str) -> np.ndarray:
    if not Path(_KOKORO_MODEL).exists():
        print(f"  warn: Kokoro not found at {_KOKORO_MODEL} — using 2s 440 Hz tone", flush=True)
        t = np.linspace(0, 2, _SAMPLE_RATE * 2, dtype=np.float32)
        return (0.15 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    import asyncio
    from companion_harness.tts_kokoro import KokoroTtsAdapter
    adapter = KokoroTtsAdapter(model_path=_KOKORO_MODEL, voices_path=_KOKORO_VOICES, warmup=False)
    print(f"  Kokoro loaded; synthesizing {len(phrase)} char phrase...", flush=True)

    async def _collect() -> list[bytes]:
        return [c async for c in adapter.synthesize(phrase, [])]

    chunks = asyncio.get_event_loop().run_until_complete(_collect())
    if not chunks:
        return np.zeros(_SAMPLE_RATE * 2, dtype=np.float32)
    pcm16 = np.frombuffer(b"".join(chunks), dtype="<i2").astype(np.float32) / 32768.0
    return _resample_nearest(pcm16, 24000, _SAMPLE_RATE)


def _peak_normalize(audio: np.ndarray) -> np.ndarray:
    abs_max = float(np.abs(audio).max())
    if abs_max > 0.01:
        return audio * (0.9 / abs_max)
    return audio


def _rms(audio: np.ndarray) -> float:
    return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))


def _abs_max(audio: np.ndarray) -> float:
    return float(np.abs(audio).max())


# ---------------------------------------------------------------------------
# chat(stream=True) runner
# ---------------------------------------------------------------------------

def _run_variant(base, tokenizer, audio: np.ndarray, label: str) -> dict:
    # chat(stream=True) creates and returns its own internal TextIteratorStreamer.
    # Passing streamer= as a kwarg does NOT work: _decode_stream builds the
    # internal streamer first, then generation_config.update(kwargs) overwrites
    # the streamer key (so generate() uses ours), but _decode_stream returns its
    # own unused handle — giving us an empty, permanently-blocking iterator.
    # Correct usage: call chat(stream=True) in a thread, receive the returned
    # streamer, then consume it on the main thread.
    import threading

    exc_holder: list[Exception] = []
    returned_streamer_holder: list = []

    msgs = [
        {"role": "system", "content": [_DEFAULT_CHAT_STREAM_SYSTEM_PROMPT]},
        {"role": "user",   "content": [audio, _DEFAULT_USER_INSTRUCTION]},
    ]

    def _run():
        try:
            import torch
            with torch.no_grad():
                inner_streamer = base.chat(
                    msgs=msgs,
                    tokenizer=tokenizer,
                    stream=True,
                    generate_audio=False,
                    omni_mode=True,
                    max_new_tokens=_CHAT_STREAM_MAX_NEW_TOKENS,
                    temperature=_CHAT_STREAM_TEMPERATURE,
                )
            returned_streamer_holder.append(inner_streamer)
        except Exception as e:
            exc_holder.append(e)

    t = threading.Thread(target=_run, daemon=True)
    t_start = time.monotonic()
    t.start()

    # Wait for the thread to store the returned streamer (chat() returns it
    # once the background generate thread is running, before tokens arrive).
    t.join(timeout=60.0)
    if exc_holder:
        elapsed_ms = (time.monotonic() - t_start) * 1000
        return {
            "label": label,
            "rms": round(_rms(audio), 4),
            "abs_max": round(_abs_max(audio), 4),
            "chars": 0,
            "raw_tokens": 0,
            "raw_preview": "",
            "ttft_ms": None,
            "elapsed_ms": round(elapsed_ms),
            "timeout_hit": False,
            "text_preview": "",
            "verdict": "ERROR",
            "error": str(exc_holder[0]),
        }
    if not returned_streamer_holder:
        elapsed_ms = (time.monotonic() - t_start) * 1000
        return {
            "label": label,
            "rms": round(_rms(audio), 4),
            "abs_max": round(_abs_max(audio), 4),
            "chars": 0, "raw_tokens": 0, "raw_preview": "",
            "ttft_ms": None, "elapsed_ms": round(elapsed_ms),
            "timeout_hit": True, "text_preview": "", "verdict": "ERROR",
            "error": "chat() did not return streamer within 60s",
        }

    streamer = returned_streamer_holder[0]

    import re as _re
    _special = _re.compile(r"<\|[^|]*\|>")

    import queue as _queue
    parts: list[str] = []
    raw_tokens: list[str] = []
    ttft_ms: float | None = None
    timeout_hit = False
    while True:
        try:
            raw = next(streamer)
        except StopIteration:
            break
        except _queue.Empty:
            timeout_hit = True
            break
        if raw is None:
            break
        raw_tokens.append(raw)
        cleaned = _special.sub("", raw).strip()
        if cleaned:
            if ttft_ms is None:
                ttft_ms = (time.monotonic() - t_start) * 1000
            parts.append(cleaned)

    t.join(timeout=10.0)
    elapsed_ms = (time.monotonic() - t_start) * 1000

    full_text = "".join(parts)
    raw_preview = "".join(raw_tokens)[:200]
    result = {
        "label": label,
        "rms": round(_rms(audio), 4),
        "abs_max": round(_abs_max(audio), 4),
        "chars": len(full_text),
        "raw_tokens": len(raw_tokens),
        "raw_preview": raw_preview,
        "ttft_ms": round(ttft_ms) if ttft_ms is not None else None,
        "elapsed_ms": round(elapsed_ms),
        "timeout_hit": timeout_hit,
        "text_preview": full_text[:120],
        "verdict": "PASS" if len(full_text) >= 20 else "FAIL",
    }
    if exc_holder:
        result["error"] = str(exc_holder[0])
        result["verdict"] = "ERROR"
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="MiniCPM-o chat-stream amplitude attenuation probe")
    parser.add_argument("--output", default="/tmp/probe-attenuation.json",
                        help="Path for JSON results (default: /tmp/probe-attenuation.json)")
    args = parser.parse_args()

    # --- VRAM pre-check ---
    try:
        import torch
    except ImportError:
        print("ERROR: torch not importable", flush=True)
        return 1
    if not torch.cuda.is_available():
        print("ERROR: no CUDA device", flush=True)
        return 1
    free_gb = torch.cuda.mem_get_info()[0] / 1e9
    print(f"CUDA free VRAM: {free_gb:.1f} GB", flush=True)
    if free_gb < 10.0:
        print("GPU busy (free VRAM < 10 GB) — stop the console first, then re-run.", flush=True)
        return 1

    # --- synthesize baseline audio ---
    print("\nSynthesizing Kokoro fixture...", flush=True)
    baseline = _synthesize(_TEST_PHRASE)
    dur_s = len(baseline) / _SAMPLE_RATE
    print(f"  baseline: {dur_s:.1f}s  RMS={_rms(baseline):.4f}  abs_max={_abs_max(baseline):.4f}", flush=True)

    variants: list[tuple[str, np.ndarray]] = [
        ("baseline",                  baseline.copy()),
        ("attenuated_0.05x",          baseline * 0.05),
        ("attenuated_then_peak_norm", _peak_normalize(baseline * 0.05)),
        ("attenuated_then_5x_gain",   baseline * 0.05 * 5.0),
    ]

    # --- load model ---
    print("\nLoading MiniCPMStreamingModel (init_vision=False)...", flush=True)
    t0 = time.monotonic()
    try:
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
        model = MiniCPMStreamingModel(init_vision=False)
    except torch.cuda.OutOfMemoryError:
        print("OOM — stop console first, then re-run.", flush=True)
        return 1
    except Exception as exc:
        print(f"Model load failed: {type(exc).__name__}: {exc}", flush=True)
        return 1
    print(f"  loaded in {round((time.monotonic() - t0) * 1000)} ms", flush=True)

    base = model._base
    tokenizer = model._tokenizer

    # --- run variants sequentially ---
    results: list[dict] = []
    for label, audio in variants:
        rms_val  = _rms(audio)
        amax_val = _abs_max(audio)
        print(f"\n{'='*60}", flush=True)
        print(f"Variant: {label}", flush=True)
        print(f"  RMS={rms_val:.4f}  abs_max={amax_val:.4f}", flush=True)
        model.reset_streaming_session(caused_by=[])
        r = _run_variant(base, tokenizer, audio, label)
        results.append(r)
        verdict_str = r["verdict"]
        chars = r["chars"]
        ttft  = r["ttft_ms"]
        raw_n = r.get("raw_tokens", "?")
        tmo   = r.get("timeout_hit", False)
        print(f"  chars={chars}  raw_tokens={raw_n}  ttft={ttft}ms  timeout={tmo}  verdict={verdict_str}", flush=True)
        if r.get("raw_preview"):
            print(f"  raw_preview: {r['raw_preview']!r}", flush=True)
        if r.get("text_preview"):
            print(f"  text_preview: {r['text_preview']!r}", flush=True)
        if r.get("error"):
            print(f"  error: {r['error']}", flush=True)

    # --- save JSON ---
    Path(args.output).write_text(json.dumps({"variants": results}, indent=2), encoding="utf-8")
    print(f"\nResults written to {args.output}", flush=True)

    # --- verdict table ---
    print("\n" + "=" * 70)
    print("VERDICT TABLE")
    print("=" * 70)
    print(f"{'Variant':<32} {'RMS':>6} {'chars':>6} {'ttft':>8}  {'verdict'}")
    print("-" * 70)
    for r in results:
        ttft_str = f"{r['ttft_ms']}ms" if r["ttft_ms"] is not None else "N/A"
        print(f"{r['label']:<32} {r['rms']:>6.4f} {r['chars']:>6} {ttft_str:>8}  {r['verdict']}")
    print("=" * 70)

    any_fail = any(r["verdict"] != "PASS" for r in results)
    return 1 if any_fail else 0


if __name__ == "__main__":
    sys.exit(main())
