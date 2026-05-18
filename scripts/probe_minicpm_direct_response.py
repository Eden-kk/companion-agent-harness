"""Probe: MiniCPM-o duplex direct response — model-alone baseline.

Bypasses the entire orchestrator (no EventLogger, no policy gate, no
AudioOutputController). Feeds synthesized audio directly to MiniCPMStreamingModel
and records text + audio output so we can distinguish orchestration bugs from
model bugs.

Usage:
    /raid/yid042/venvs/companion-harness/bin/python \
        scripts/probe_minicpm_direct_response.py [--input path/to/input.wav]

Outputs:
    /tmp/probe_minicpm_direct_response.wav   — model audio response (if any)
    /tmp/probe_minicpm_direct_response.txt   — model text response

Exit: 0=model produced text+audio, 1=nothing produced or error.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path

import numpy as np


_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 16000  # 1-second chunks

_KOKORO_DEFAULT_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_DEFAULT_VOICES = "/raid/yid042/models/kokoro/voices.json"
_TEST_PHRASE = "Hello, how are you today?"

_OUT_WAV = "/tmp/probe_minicpm_direct_response.wav"
_OUT_TXT = "/tmp/probe_minicpm_direct_response.txt"


# ---------------------------------------------------------------------------
# WAV helpers (no soundfile dependency required)
# ---------------------------------------------------------------------------

def _write_wav(path: str, samples: np.ndarray, sample_rate: int) -> None:
    """Write float32 [-1,1] samples as 16-bit PCM WAV."""
    pcm16 = (np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2")
    data = pcm16.tobytes()
    n_channels, sampwidth, n_frames = 1, 2, len(pcm16)
    byte_rate = sample_rate * n_channels * sampwidth
    block_align = n_channels * sampwidth
    with open(path, "wb") as f:
        # RIFF header
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(data)))
        f.write(b"WAVE")
        # fmt chunk
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, n_channels, sample_rate,
                            byte_rate, block_align, sampwidth * 8))
        # data chunk
        f.write(b"data")
        f.write(struct.pack("<I", len(data)))
        f.write(data)


def _read_wav_as_float32(path: str) -> tuple[np.ndarray, int]:
    """Read a WAV file (PCM16 or float32) into float32 [-1,1] array."""
    import wave
    with wave.open(path, "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sampwidth == 2:
        samples = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 4:
        samples = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2**31
    else:
        raise ValueError(f"unsupported sampwidth {sampwidth}")
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)
    return samples, rate


def _resample_nearest(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    if src_rate == dst_rate:
        return audio
    out_len = int(len(audio) * dst_rate / src_rate)
    indices = np.round(np.arange(out_len) * src_rate / dst_rate).astype(int)
    indices = np.clip(indices, 0, len(audio) - 1)
    return audio[indices]


# ---------------------------------------------------------------------------
# Input synthesis / load
# ---------------------------------------------------------------------------

def _synthesize_input(phrase: str) -> np.ndarray:
    """Synthesize phrase via Kokoro and return float32 16kHz PCM."""
    import asyncio
    model_path = _KOKORO_DEFAULT_MODEL
    voices_path = _KOKORO_DEFAULT_VOICES
    if not Path(model_path).exists() or not Path(voices_path).exists():
        print(f"  warn: Kokoro not found at {model_path} — using 2s silence", flush=True)
        return np.zeros(_SAMPLE_RATE * 2, dtype=np.float32)

    from companion_harness.tts_kokoro import KokoroTtsAdapter
    adapter = KokoroTtsAdapter(model_path=model_path, voices_path=voices_path, warmup=False)
    print(f"  Kokoro loaded, synthesizing: {phrase!r}", flush=True)

    async def _collect() -> list[bytes]:
        chunks = []
        async for chunk in adapter.synthesize(phrase, []):
            chunks.append(chunk)
        return chunks

    raw_chunks = asyncio.get_event_loop().run_until_complete(_collect())
    if not raw_chunks:
        return np.zeros(_SAMPLE_RATE * 2, dtype=np.float32)
    pcm16 = np.frombuffer(b"".join(raw_chunks), dtype="<i2").astype(np.float32) / 32768.0
    # Kokoro emits 24kHz; resample to 16kHz
    return _resample_nearest(pcm16, 24000, _SAMPLE_RATE)


def _load_input(wav_path: str | None) -> np.ndarray:
    if wav_path:
        print(f"Loading input WAV: {wav_path}", flush=True)
        audio, rate = _read_wav_as_float32(wav_path)
        return _resample_nearest(audio, rate, _SAMPLE_RATE)
    print("No --input given; synthesizing test phrase via Kokoro...", flush=True)
    return _synthesize_input(_TEST_PHRASE)


# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

def _terminator_name(duplex, token_id: int) -> str:
    if token_id == duplex.listen_token_id:
        return "listen"
    if token_id == duplex.chunk_eos_token_id:
        return "chunk_eos"
    if token_id == duplex.chunk_tts_eos_token_id:
        return "chunk_tts_eos"
    return f"other:{token_id}"


def _process_chunk(duplex, audio: np.ndarray) -> dict:
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
    if duplex.total_ids:
        result["_terminator"] = _terminator_name(duplex, duplex.total_ids[-1])
    else:
        result["_terminator"] = "unknown"
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="MiniCPM-o direct response probe")
    parser.add_argument("--input", metavar="WAV", default=None,
                        help="Path to input WAV file (default: synthesize test phrase)")
    args = parser.parse_args()

    # --- load input audio first (fast, no GPU needed) ---
    input_audio = _load_input(args.input)
    duration_s = len(input_audio) / _SAMPLE_RATE
    n_chunks = int(np.ceil(len(input_audio) / _CHUNK_SAMPLES))
    print(f"Input: {duration_s:.2f}s ({len(input_audio)} samples, {n_chunks} chunk(s))", flush=True)

    # --- load model ---
    print("\nLoading MiniCPMStreamingModel (init_tts=True inside adapter)...", flush=True)
    t_load_start = time.monotonic()
    try:
        import torch
        if not torch.cuda.is_available():
            print("ERROR: no CUDA device found", flush=True)
            return 1
        # Quick OOM pre-check: if another process holds most of VRAM we'll OOM on load.
        free_gb = torch.cuda.mem_get_info()[0] / 1e9
        print(f"  CUDA free VRAM: {free_gb:.1f} GB", flush=True)
        if free_gb < 10.0:
            print("GPU busy (free VRAM < 10 GB) — stop the console first, then re-run.", flush=True)
            return 1

        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
        model = MiniCPMStreamingModel()
    except torch.cuda.OutOfMemoryError:
        print("GPU busy (OutOfMemoryError) — stop console first, then re-run.", flush=True)
        return 1
    except Exception as exc:
        print(f"Model load failed: {type(exc).__name__}: {exc}", flush=True)
        return 1

    t_loaded = time.monotonic()
    print(f"  model loaded in {round((t_loaded - t_load_start) * 1000)} ms", flush=True)

    from companion_harness.foreground_model_minicpm import _DEFAULT_DUPLEX_SYSTEM_PROMPT
    duplex = model._duplex
    duplex.prepare(prefix_system_prompt=_DEFAULT_DUPLEX_SYSTEM_PROMPT)

    # --- feed audio chunks ---
    print("\nFeeding audio chunks...", flush=True)
    text_parts: list[str] = []
    audio_parts: list[np.ndarray] = []
    t_first_token: float | None = None
    t_first_audio: float | None = None
    t_feed_start = time.monotonic()

    buf = input_audio.copy()
    chunk_idx = 0
    while len(buf) > 0:
        if len(buf) >= _CHUNK_SAMPLES:
            chunk, buf = buf[:_CHUNK_SAMPLES], buf[_CHUNK_SAMPLES:]
        else:
            pad = np.zeros(_CHUNK_SAMPLES - len(buf), dtype=np.float32)
            chunk = np.concatenate([buf, pad])
            buf = np.array([], dtype=np.float32)

        chunk_idx += 1
        t_chunk = time.monotonic()
        try:
            result = _process_chunk(duplex, chunk)
        except Exception as exc:
            print(f"  chunk {chunk_idx}: ERROR {type(exc).__name__}: {exc}", flush=True)
            break

        text = result.get("text", "")
        is_listen = bool(result.get("is_listen", True))
        wav = result.get("audio_waveform")
        terminator = result.get("_terminator", "unknown")

        if text and t_first_token is None:
            t_first_token = time.monotonic() - t_feed_start
        if wav is not None and len(wav) > 0 and t_first_audio is None:
            t_first_audio = time.monotonic() - t_feed_start

        if text:
            text_parts.append(text)
        if wav is not None and len(wav) > 0:
            audio_parts.append(wav)

        print(f"  chunk {chunk_idx}/{n_chunks}: terminator={terminator}  is_listen={is_listen}  "
              f"text={text[:60]!r}  "
              f"audio={'yes' if wav is not None and len(wav) > 0 else 'no'}", flush=True)

    t_done = time.monotonic()
    total_response_s = t_done - t_feed_start

    # --- save outputs ---
    full_text = " ".join(text_parts).strip()
    Path(_OUT_TXT).write_text(full_text, encoding="utf-8")

    output_wav_path = "none"
    if audio_parts:
        combined = np.concatenate(audio_parts)
        # native TTS runs at 24kHz; keep as-is and label accordingly
        _write_wav(_OUT_WAV, combined, 24000)
        output_wav_path = _OUT_WAV

    # --- summary ---
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Input duration       : {duration_s:.2f}s")
    print(f"  Chunks fed           : {n_chunks}")
    print(f"  First-token latency  : {round(t_first_token * 1000) if t_first_token else 'N/A'} ms")
    print(f"  First-audio latency  : {round(t_first_audio * 1000) if t_first_audio else 'N/A'} ms")
    print(f"  Total response time  : {round(total_response_s * 1000)} ms")
    print(f"  Text ({len(full_text)} chars)     : {full_text[:200]!r}")
    print(f"  Text saved to        : {_OUT_TXT}")
    print(f"  Audio saved to       : {output_wav_path}")
    print("=" * 60)

    if not full_text and not audio_parts:
        print("\nVERDICT: NOTHING — model produced no text and no audio.", flush=True)
        return 1
    print("\nVERDICT: OK — model produced output.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
