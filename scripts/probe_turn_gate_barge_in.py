"""Probe: MiniCPM-o turn-gate barge-in — does the model produce a usable
mid-turn yield signal, and does it yield coherently if the gate is bypassed?

Background
----------
modeling_minicpmo.py:3215 gates the model's own `<|listen|>` token mid-turn:

    last_id = self.decoder.decode(...)              # model SAMPLES its judgment
    if last_id.item() == self.listen_token_id and (not self.current_turn_ended):
        last_id = tts_bos_token_id                  # gate DISCARDS it -> keep speaking

This is the make-or-break experiment for a turn-free continuous companion.
Two questions:

  RUN A (observational, gate intact):
    While the model is mid-utterance and a barge-in arrives, how often does
    decode() return `<|listen|>` (a yield the gate then suppresses)?
    >0 and rising during barge-in  => the latent yield signal EXISTS.

  RUN B (interventional, gate bypassed via data-descriptor on current_turn_ended):
    With the gate open, does the model actually yield (is_listen=True) during
    the barge-in, and at a coherent point (after a clause, not mid-word)?
    coherent yield => turn-free is an ORCHESTRATION problem.
    garbage/no-yield => needs fine-tuning; MiniCPM-o alone insufficient.

Feeds: a long-response prompt (model speaks for several chunks) then, once the
model is speaking, a barge-in utterance, then silence.

Usage:
    CUDA_VISIBLE_DEVICES=1 /raid/yid042/venvs/companion-harness/bin/python \
        scripts/probe_turn_gate_barge_in.py

Outputs:
    /tmp/probe-turn-gate-barge-in.json   — full per-chunk logs, both runs
    /tmp/probe-turn-gate-summary.txt     — verdict table
"""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path

import numpy as np

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 16000  # 1-second chunks

_KOKORO_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_VOICES = "/raid/yid042/models/kokoro/voices.json"

# Prompt chosen to elicit a multi-sentence (multi-chunk) spoken response so the
# barge-in lands while the model is mid-utterance.
_PROMPT_TEXT = "Please tell me a long detailed story about a journey to the moon and what the traveler saw there."
# Clear, semantically strong interruption.
_BARGE_IN_TEXT = "No wait, stop, I changed my mind, let's talk about something else."

_MAX_AWAIT_SPEAK_CHUNKS = 10   # silence chunks to wait for model to start speaking
_MAX_POST_CHUNKS = 12          # chunks to observe after barge-in starts


# ---------------------------------------------------------------------------
# Audio synthesis (Kokoro), with graceful silence fallback
# ---------------------------------------------------------------------------

def _resample_nearest(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio
    out_len = int(len(audio) * dst / src)
    idx = np.clip(np.round(np.arange(out_len) * src / dst).astype(int), 0, len(audio) - 1)
    return audio[idx]


def _synthesize(phrase: str) -> np.ndarray:
    import asyncio
    if not (Path(_KOKORO_MODEL).exists() and Path(_KOKORO_VOICES).exists()):
        print(f"  warn: Kokoro missing — using 2s silence for {phrase!r}", flush=True)
        return np.zeros(_SAMPLE_RATE * 2, dtype=np.float32)
    from companion_harness.tts_kokoro import KokoroTtsAdapter
    adapter = KokoroTtsAdapter(model_path=_KOKORO_MODEL, voices_path=_KOKORO_VOICES, warmup=False)

    async def _collect() -> list[bytes]:
        out = []
        async for c in adapter.synthesize(phrase, []):
            out.append(c)
        return out

    chunks = asyncio.new_event_loop().run_until_complete(_collect())
    if not chunks:
        return np.zeros(_SAMPLE_RATE, dtype=np.float32)
    pcm = np.frombuffer(b"".join(chunks), dtype="<i2").astype(np.float32) / 32768.0
    return _resample_nearest(pcm, 24000, _SAMPLE_RATE)


# ---------------------------------------------------------------------------
# Chunk processing + decode instrumentation
# ---------------------------------------------------------------------------

def _process_chunk(duplex, audio: np.ndarray) -> dict:
    duplex.streaming_prefill(audio_waveform=audio)
    return duplex.streaming_generate(
        max_new_speak_tokens_per_chunk=duplex.max_new_speak_tokens_per_chunk,
        temperature=duplex.temperature,
        top_k=duplex.top_k,
        top_p=duplex.top_p,
        listen_prob_scale=duplex.listen_prob_scale,
        text_repetition_penalty=duplex.text_repetition_penalty,
        text_repetition_window_size=duplex.text_repetition_window_size,
    )


def _install_decode_probe(duplex) -> list[dict]:
    """Wrap decoder.decode to log every sampled token + the gate-relevant state.

    Returns a list that accumulates one record per decode() call:
      {token, is_listen_token, current_turn_ended}
    A 'suppressed yield' = is_listen_token AND not current_turn_ended.
    """
    log: list[dict] = []
    orig = duplex.decoder.decode
    listen_id = duplex.listen_token_id

    def wrapped(*args, **kwargs):
        out = orig(*args, **kwargs)
        try:
            tok = int(out.item())
        except Exception:
            tok = -1
        log.append({
            "token": tok,
            "is_listen_token": tok == listen_id,
            # value the gate at :3216 will read this same iteration
            "current_turn_ended": bool(duplex.current_turn_ended),
        })
        return out

    wrapped.__wrapped__ = orig
    duplex.decoder.decode = wrapped
    return log


class _AlwaysEnded:
    """Data descriptor: current_turn_ended always True, writes ignored.

    A data descriptor on the class takes precedence over the instance __dict__,
    so this opens the mid-turn listen gate at modeling_minicpmo.py:3216 without
    copying streaming_generate(). Writes (self.current_turn_ended = False/True)
    become no-ops.
    """

    def __get__(self, obj, objtype=None):
        return True

    def __set__(self, obj, value):
        pass


# ---------------------------------------------------------------------------
# One run (prompt -> await speak -> barge-in -> observe)
# ---------------------------------------------------------------------------

def run_session(duplex, system_prompt: str, prompt_audio: np.ndarray,
                barge_audio: np.ndarray, label: str) -> dict:
    print(f"\n{'='*64}\nRUN {label}\n{'='*64}", flush=True)
    duplex.prepare(prefix_system_prompt=system_prompt)
    decode_log = _install_decode_probe(duplex)

    def n_chunks(a):
        return int(np.ceil(len(a) / _CHUNK_SAMPLES))

    silence = np.zeros(_CHUNK_SAMPLES, dtype=np.float32)
    prompt_buf = prompt_audio.copy()
    barge_buf = barge_audio.copy()

    per_chunk: list[dict] = []
    phase = "prompt"
    chunk_idx = 0
    await_count = 0
    post_count = 0
    speaking_seen = False
    first_speak_chunk = None
    barge_started_chunk = None
    first_yield_after_barge = None
    text_before_barge: list[str] = []
    text_during_after_barge: list[str] = []

    while True:
        chunk_idx += 1
        # ---- pick the chunk to feed based on phase ----
        if phase == "prompt":
            if len(prompt_buf) >= _CHUNK_SAMPLES:
                chunk, prompt_buf = prompt_buf[:_CHUNK_SAMPLES], prompt_buf[_CHUNK_SAMPLES:]
            elif len(prompt_buf) > 0:
                pad = np.zeros(_CHUNK_SAMPLES - len(prompt_buf), dtype=np.float32)
                chunk = np.concatenate([prompt_buf, pad])
                prompt_buf = np.array([], dtype=np.float32)
            else:
                phase = "await_speak"
                chunk = silence
            kind = "prompt" if phase == "prompt" else "await_speak"
        elif phase == "await_speak":
            chunk = silence
            kind = "await_speak"
        elif phase == "barge_in":
            if len(barge_buf) >= _CHUNK_SAMPLES:
                chunk, barge_buf = barge_buf[:_CHUNK_SAMPLES], barge_buf[_CHUNK_SAMPLES:]
            elif len(barge_buf) > 0:
                pad = np.zeros(_CHUNK_SAMPLES - len(barge_buf), dtype=np.float32)
                chunk = np.concatenate([barge_buf, pad])
                barge_buf = np.array([], dtype=np.float32)
            else:
                phase = "post"
                chunk = silence
            kind = "barge_in" if phase == "barge_in" else "post"
        else:  # post
            chunk = silence
            kind = "post"

        # ---- run the chunk, capturing decode records for THIS chunk ----
        decode_before = len(decode_log)
        try:
            result = _process_chunk(duplex, chunk)
        except Exception as exc:
            print(f"  chunk {chunk_idx}: ERROR {type(exc).__name__}: {exc}", flush=True)
            break
        chunk_decodes = decode_log[decode_before:]

        is_listen = bool(result.get("is_listen", True))
        text = result.get("text", "") or ""
        raw_listen = sum(1 for d in chunk_decodes if d["is_listen_token"])
        suppressed = sum(1 for d in chunk_decodes
                         if d["is_listen_token"] and not d["current_turn_ended"])

        if not is_listen:
            speaking_seen = True
            if first_speak_chunk is None:
                first_speak_chunk = chunk_idx

        # bookkeeping by phase
        if kind in ("prompt", "await_speak"):
            if text:
                text_before_barge.append(text)
        else:
            if text:
                text_during_after_barge.append(text)
            if is_listen and barge_started_chunk is not None and first_yield_after_barge is None:
                first_yield_after_barge = chunk_idx

        per_chunk.append({
            "chunk_idx": chunk_idx,
            "phase": kind,
            "is_listen": is_listen,
            "raw_listen_samples": raw_listen,
            "suppressed_yields": suppressed,
            "n_decodes": len(chunk_decodes),
            "text": text,
        })
        print(f"  ch{chunk_idx:>2} [{kind:<11}] listen={int(is_listen)} "
              f"raw_listen={raw_listen} suppressed={suppressed} "
              f"text={text[:42]!r}", flush=True)

        # ---- phase transitions ----
        if phase == "await_speak":
            await_count += 1
            # once the model is speaking, start the barge-in next chunk
            if speaking_seen:
                phase = "barge_in"
                barge_started_chunk = chunk_idx + 1
            elif await_count >= _MAX_AWAIT_SPEAK_CHUNKS:
                print("  => model never started speaking; aborting run", flush=True)
                break
        elif phase == "post":
            post_count += 1
            if post_count >= _MAX_POST_CHUNKS:
                break

    # restore decode
    duplex.decoder.decode = duplex.decoder.decode.__wrapped__ if hasattr(
        duplex.decoder.decode, "__wrapped__") else duplex.decoder.decode

    barge_chunks = [c for c in per_chunk if c["phase"] in ("barge_in", "post")]
    return {
        "label": label,
        "first_speak_chunk": first_speak_chunk,
        "barge_started_chunk": barge_started_chunk,
        "first_yield_after_barge": first_yield_after_barge,
        "suppressed_yields_total": sum(c["suppressed_yields"] for c in per_chunk),
        "suppressed_yields_during_barge": sum(c["suppressed_yields"] for c in barge_chunks),
        "raw_listen_during_barge": sum(c["raw_listen_samples"] for c in barge_chunks),
        "yielded_during_barge": first_yield_after_barge is not None,
        "text_before_barge": " ".join(text_before_barge).strip(),
        "text_during_after_barge": " ".join(text_during_after_barge).strip(),
        "per_chunk": per_chunk,
    }


def main() -> int:
    import torch
    if not torch.cuda.is_available():
        print("ERROR: no CUDA", flush=True)
        return 1
    free_gb = torch.cuda.mem_get_info()[0] / 1e9
    print(f"CUDA free VRAM: {free_gb:.1f} GB", flush=True)
    if free_gb < 10.0:
        print("GPU busy (<10GB free) — pick a free device via CUDA_VISIBLE_DEVICES.", flush=True)
        return 1

    print("Loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import (
        MiniCPMStreamingModel, _DEFAULT_DUPLEX_SYSTEM_PROMPT,
    )
    model = MiniCPMStreamingModel()
    duplex = model._duplex
    print(f"  loaded in {round((time.monotonic()-t0)*1000)} ms", flush=True)

    print(f"\nSynthesizing prompt + barge-in via Kokoro...", flush=True)
    prompt_audio = _synthesize(_PROMPT_TEXT)
    barge_audio = _synthesize(_BARGE_IN_TEXT)
    print(f"  prompt {len(prompt_audio)/_SAMPLE_RATE:.2f}s, barge-in {len(barge_audio)/_SAMPLE_RATE:.2f}s", flush=True)

    # RUN A — gate intact (observational)
    run_a = run_session(duplex, _DEFAULT_DUPLEX_SYSTEM_PROMPT,
                        prompt_audio, barge_audio, "A (gate intact)")

    # RUN B — gate bypassed via data descriptor on current_turn_ended
    print("\nInstalling _AlwaysEnded descriptor (opens mid-turn listen gate)...", flush=True)
    type(duplex).current_turn_ended = _AlwaysEnded()
    try:
        run_b = run_session(duplex, _DEFAULT_DUPLEX_SYSTEM_PROMPT,
                            prompt_audio, barge_audio, "B (gate bypassed)")
    finally:
        del type(duplex).current_turn_ended  # restore instance-attr behavior

    out = {
        "prompt_text": _PROMPT_TEXT,
        "barge_in_text": _BARGE_IN_TEXT,
        "run_a_gate_intact": run_a,
        "run_b_gate_bypassed": run_b,
    }
    Path("/tmp/probe-turn-gate-barge-in.json").write_text(json.dumps(out, indent=2), encoding="utf-8")

    # ---- verdict ----
    a_sig = run_a["suppressed_yields_during_barge"]
    a_raw = run_a["raw_listen_during_barge"]
    b_yield = run_b["yielded_during_barge"]
    lines = [
        "probe_turn_gate_barge_in summary",
        "",
        f"prompt:   {_PROMPT_TEXT!r}",
        f"barge-in: {_BARGE_IN_TEXT!r}",
        "",
        "RUN A (gate intact, observational):",
        f"  first_speak_chunk            = {run_a['first_speak_chunk']}",
        f"  barge_started_chunk          = {run_a['barge_started_chunk']}",
        f"  raw <|listen|> samples during barge window = {a_raw}",
        f"  SUPPRESSED yields during barge window      = {a_sig}",
        f"  text before barge: {run_a['text_before_barge'][:80]!r}",
        "",
        "RUN B (gate bypassed, interventional):",
        f"  first_speak_chunk            = {run_b['first_speak_chunk']}",
        f"  barge_started_chunk          = {run_b['barge_started_chunk']}",
        f"  first_yield_after_barge      = {run_b['first_yield_after_barge']}",
        f"  yielded during barge?        = {b_yield}",
        f"  text before barge: {run_b['text_before_barge'][:80]!r}",
        f"  text during/after barge: {run_b['text_during_after_barge'][:80]!r}",
        "",
        "VERDICT:",
    ]
    if a_sig > 0 and b_yield:
        lines.append("  GO — latent mid-turn yield signal EXISTS and the model yields when")
        lines.append("       the gate is opened. Turn-free barge-in is an ORCHESTRATION problem.")
        lines.append("       (Inspect text coherence above before fully committing.)")
    elif a_sig > 0 and not b_yield:
        lines.append("  MIXED — model samples <|listen|> mid-turn (signal exists) but did not")
        lines.append("          yield when gate opened. May need listen_prob_scale tuning.")
    elif a_sig == 0 and a_raw == 0:
        lines.append("  NO-GO — model never samples <|listen|> mid-turn under barge-in.")
        lines.append("          The yield signal does NOT exist; needs fine-tuning, not orchestration.")
    else:
        lines.append("  INCONCLUSIVE — see per-chunk logs in the JSON.")

    summary = "\n".join(lines)
    print("\n" + summary, flush=True)
    Path("/tmp/probe-turn-gate-summary.txt").write_text(summary + "\n", encoding="utf-8")
    print("\n=> /tmp/probe-turn-gate-barge-in.json + /tmp/probe-turn-gate-summary.txt", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
