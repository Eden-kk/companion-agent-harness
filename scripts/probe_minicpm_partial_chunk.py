"""Probe: MiniCPM-o duplex partial-chunk tolerance gate (Path B Option A fix).

Run on B200:

    /raid/yid042/venvs/companion-harness/bin/python scripts/probe_minicpm_partial_chunk.py

Tests whether streaming_prefill + streaming_generate produce intelligible output
when fed sub-1-second audio buffers (0.3s, 0.5s, 0.8s). This gates the
EOU-triggered partial-flush approach in Path B Option A.

SHIP  — all 15 cells return non-empty text and no errors.
NO-SHIP — any cell errors, returns empty, or hangs.
HUMAN-REVIEW — non-empty output with quality concerns (repetition, non-ASCII soup).

Exit: 0=SHIP, 1=NO-SHIP, 2=HUMAN-REVIEW.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import numpy as np


_SAMPLE_RATE = 16000  # Hz, matches MiniCPM-o duplex contract
_CHUNK_SAMPLES = 16000  # 1.0s full chunk

# Sub-chunk sizes under test
_PROBE_SIZES = [
    (4800, "0.3s"),
    (8000, "0.5s"),
    (12800, "0.8s"),
]

_FIXTURES = [
    "Hello.",
    "What's your name?",
    "Stop.",
    "Yes please.",
    "Why?",
]

_KOKORO_DEFAULT_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_DEFAULT_VOICES = "/raid/yid042/models/kokoro/voices.json"

# Garbage-detection: if >40% of a response is repeated 3-grams it's likely degenerate
_REPETITION_THRESHOLD = 0.40
# Non-ASCII fraction threshold (English-only fixtures)
_NONASCII_THRESHOLD = 0.15


def _is_garbage(text: str) -> bool:
    """Heuristic check for degenerate model output."""
    if not text:
        return False
    # Check non-ASCII fraction
    nonascii = sum(1 for c in text if ord(c) > 127)
    if nonascii / len(text) > _NONASCII_THRESHOLD:
        return True
    # Check 3-gram repetition
    tokens = text.split()
    if len(tokens) < 6:
        return False
    trigrams = [tuple(tokens[i:i+3]) for i in range(len(tokens) - 2)]
    unique = len(set(trigrams))
    if unique / len(trigrams) < (1 - _REPETITION_THRESHOLD):
        return True
    return False


def _load_kokoro() -> object | None:
    """Load KokoroTtsAdapter. Returns None if unavailable (falls back to silence)."""
    model_path = os.environ.get("KOKORO_MODEL_PATH", _KOKORO_DEFAULT_MODEL)
    voices_path = os.environ.get("KOKORO_VOICES_PATH", _KOKORO_DEFAULT_VOICES)
    if not Path(model_path).exists() or not Path(voices_path).exists():
        print(f"  warn: Kokoro model not found at {model_path} — using silence fallback", flush=True)
        return None
    try:
        from companion_harness.tts_kokoro import KokoroTtsAdapter  # noqa: WPS433
        adapter = KokoroTtsAdapter(model_path=model_path, voices_path=voices_path, warmup=False)
        print(f"  Kokoro loaded from {model_path}", flush=True)
        return adapter
    except Exception as exc:
        print(f"  warn: Kokoro load failed ({type(exc).__name__}: {exc}) — using silence fallback", flush=True)
        return None


def _synthesize_fixture(kokoro: object | None, text: str, n_samples: int) -> np.ndarray:
    """Synthesize `text` via Kokoro, convert to float32 16kHz, truncate/pad to n_samples.

    Falls back to silence if Kokoro is None.
    """
    if kokoro is None:
        return np.zeros(n_samples, dtype=np.float32)

    async def _collect() -> np.ndarray:
        chunks: list[np.ndarray] = []
        async for pcm16_bytes in kokoro.synthesize(text, []):  # type: ignore[attr-defined]
            samples = np.frombuffer(pcm16_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            chunks.append(samples)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    # Kokoro is 24kHz; resample to 16kHz
    raw_24k = asyncio.get_event_loop().run_until_complete(_collect())
    if len(raw_24k) == 0:
        return np.zeros(n_samples, dtype=np.float32)

    # Simple nearest-neighbour downsample 24kHz -> 16kHz (ratio 2/3)
    ratio = _SAMPLE_RATE / 24000.0
    out_len = int(len(raw_24k) * ratio)
    indices = np.round(np.arange(out_len) / ratio).astype(int)
    indices = np.clip(indices, 0, len(raw_24k) - 1)
    resampled = raw_24k[indices]

    # Truncate or zero-pad at end to hit n_samples
    if len(resampled) >= n_samples:
        return resampled[:n_samples]
    pad = np.zeros(n_samples - len(resampled), dtype=np.float32)
    return np.concatenate([resampled, pad])


def _process_chunk_direct(model: object, audio: np.ndarray) -> dict:
    """Call streaming_prefill + streaming_generate directly on _duplex, mirroring
    the _process_chunk closure in MiniCPMStreamingModel.infer_stream.

    Returns dict with keys: text, is_listen, error.
    """
    duplex = model._duplex  # type: ignore[attr-defined]
    try:
        duplex.streaming_prefill(audio_waveform=audio)
        result = duplex.streaming_generate(
            max_new_speak_tokens_per_chunk=duplex.max_new_speak_tokens_per_chunk,
            temperature=duplex.temperature,
            top_k=duplex.top_k,
            top_p=duplex.top_p,
            listen_prob_scale=duplex.listen_prob_scale,
            text_repetition_penalty=duplex.text_repetition_penalty,
            text_repetition_window_size=duplex.text_repetition_window_size,
        )
        is_listen = bool(result.get("is_listen", True))
        text = result.get("text", "")
        return {"text": text, "is_listen": is_listen, "error": None}
    except Exception as exc:
        return {"text": "", "is_listen": True, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    print("probe_minicpm_partial_chunk: loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()

    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: WPS433
    model = MiniCPMStreamingModel()
    print(f"  model loaded in {round((time.monotonic() - t0) * 1000)}ms", flush=True)

    # Prepare duplex once (session setup)
    model._duplex.prepare(prefix_system_prompt="Streaming Omni Conversation.")

    print("\nLoading Kokoro TTS for fixture synthesis...", flush=True)
    kokoro = _load_kokoro()

    # Pre-synthesize all fixtures at all sizes (avoid re-synthesis per cell)
    print("\nSynthesizing fixture audio...", flush=True)
    fixture_audio: dict[tuple[int, str], np.ndarray] = {}
    for n_samples, size_label in _PROBE_SIZES:
        for text in _FIXTURES:
            audio = _synthesize_fixture(kokoro, text, n_samples)
            fixture_audio[(n_samples, text)] = audio
            print(f"  {size_label} / {text!r}: {len(audio)} samples", flush=True)

    print("\nRunning probe...\n", flush=True)

    results: list[dict] = []
    has_error = False
    has_empty = False
    has_garbage = False

    # Header
    header = f"{'buffer':<8} {'fixture':<22} {'is_listen':<10} {'text_preview':<45} {'error'}"
    print(header)
    print("-" * len(header))

    for n_samples, size_label in _PROBE_SIZES:
        for text in _FIXTURES:
            # Reset session between each cell (mirrors orchestrator pattern)
            model._duplex.model.reset_session(reset_token2wav_cache=False)
            model._duplex.prepare(prefix_system_prompt="Streaming Omni Conversation.")

            audio = fixture_audio[(n_samples, text)]
            cell = _process_chunk_direct(model, audio)
            cell["buffer_size"] = size_label
            cell["fixture"] = text
            results.append(cell)

            preview = (cell["text"][:42] + "...") if len(cell["text"]) > 45 else cell["text"]
            preview = preview.replace("\n", " ")
            err_str = cell["error"] or ""
            print(f"{size_label:<8} {text!r:<22} {str(cell['is_listen']):<10} {preview!r:<45} {err_str}")

            if cell["error"]:
                has_error = True
            if not cell["error"] and not cell["text"]:
                has_empty = True
            if _is_garbage(cell["text"]):
                has_garbage = True

    # Save JSON
    out_path = "/tmp/probe_minicpm_partial_chunk_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nRaw results saved to {out_path}")

    # Verdict
    print()
    if has_error:
        print("VERDICT: NO-SHIP — one or more cells raised an exception (see table above).")
        return 1
    if has_empty:
        print("VERDICT: NO-SHIP — one or more cells returned empty text with no error.")
        return 1
    if has_garbage:
        print("VERDICT: HUMAN-REVIEW — non-empty output but repetition/non-ASCII heuristic fired.")
        print("  Inspect the table above and /tmp/probe_minicpm_partial_chunk_results.json.")
        return 2
    print("VERDICT: SHIP — all 15 cells returned non-empty text with no errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
