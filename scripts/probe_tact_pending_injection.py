"""Probe: mid-session [PENDING: ...] text injection into MiniCPM-o duplex.

Run on B200 (this machine):

    /raid/yid042/venvs/companion-harness/bin/python scripts/probe_tact_pending_injection.py

Gate for the TACT-Bench vanilla-vs-prompted experiment
(tact-bench/experiments/vanilla-vs-prompted). That experiment injects a held
result ("[PENDING: <payload>]") into the live duplex session at item.t_available
via the model's text head, then reads the per-chunk is_listen/text gate to see
whether/when the model surfaces it.

This probe answers the one feasibility question: does
``duplex.streaming_prefill(audio_waveform=chunk, text_list=[note])`` execute
mid-session without error and let the model subsequently speak? The model source
exposes `text_list` (modeling_minicpmo.py streaming_prefill, ~L2759/L3094), so the
API exists; this confirms it at runtime in the harness's loaded model.

The held result is framed as a private ChatML *system turn* (REVISIONS R1) rather
than a bare "[PENDING: ...]" string, so the model treats it as context instead of
parroting the tag as speech.

SHIP        — the injection call succeeds (no exception, success=True) and the
              session keeps processing chunks afterward. (Observationally reports
              whether the model spoke post-injection.)
NO-SHIP     — the injection call raises or returns success=False, or a
              post-injection chunk crashes.

Exit: 0=SHIP, 1=NO-SHIP.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 16000  # 1.0s

_KOKORO_DEFAULT_MODEL = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_KOKORO_DEFAULT_VOICES = "/raid/yid042/models/kokoro/voices.json"

# Prompted (delivery-policy) system prompt (REVISIONS R1) — describes held results
# as private system notes; no literal "[PENDING: ...]" surface form for the model to
# parrot. Kept in sync with probe_tact_vanilla_vs_prompted.py.
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

# Probe scenario: user asks to be told when the deploy is done, then goes quiet;
# the held result arrives mid-silence. A working session should be able to speak it.
_USER_UTTERANCE = "Let me know the moment the deploy finishes, I'm heads down."
_USER_CHUNKS = 4          # user speaks over chunks 0..3
_INJECT_CHUNK = 5         # held result arrives at chunk 5
_TOTAL_CHUNKS = 11        # trail of silence after injection for the model to speak
# Held result as a private ChatML system turn (REVISIONS R1), not a bare tag.
_PENDING_NOTE = (
    "<|im_start|>system\n"
    "Background result now available (private system note — not user speech; "
    "do not read this note aloud verbatim). Result: deploy succeeded."
    "<|im_end|>\n"
)


def _load_kokoro() -> object | None:
    model_path = os.environ.get("KOKORO_MODEL_PATH", _KOKORO_DEFAULT_MODEL)
    voices_path = os.environ.get("KOKORO_VOICES_PATH", _KOKORO_DEFAULT_VOICES)
    if not Path(model_path).exists() or not Path(voices_path).exists():
        print(f"  warn: Kokoro not found at {model_path} — using silence for user audio", flush=True)
        return None
    try:
        from companion_harness.tts_kokoro import KokoroTtsAdapter

        adapter = KokoroTtsAdapter(model_path=model_path, voices_path=voices_path, warmup=False)
        print(f"  Kokoro loaded from {model_path}", flush=True)
        return adapter
    except Exception as exc:  # noqa: BLE001 — probe degrades to silence
        print(f"  warn: Kokoro load failed ({type(exc).__name__}: {exc}) — using silence", flush=True)
        return None


def _synthesize_16k(kokoro: object | None, text: str) -> np.ndarray:
    """Render `text` to float32 16kHz mono. Silence if Kokoro unavailable."""
    if kokoro is None:
        return np.zeros(0, dtype=np.float32)

    async def _collect() -> np.ndarray:
        chunks: list[np.ndarray] = []
        async for pcm16 in kokoro.synthesize(text, []):  # type: ignore[attr-defined]
            chunks.append(np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0)
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    raw_24k = asyncio.new_event_loop().run_until_complete(_collect())
    if len(raw_24k) == 0:
        return np.zeros(0, dtype=np.float32)
    # nearest-neighbour 24k -> 16k
    ratio = _SAMPLE_RATE / 24000.0
    out_len = int(len(raw_24k) * ratio)
    idx = np.clip(np.round(np.arange(out_len) / ratio).astype(int), 0, len(raw_24k) - 1)
    return raw_24k[idx]


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


def main() -> int:
    print("probe_tact_pending_injection: loading MiniCPMStreamingModel...", flush=True)
    t0 = time.monotonic()
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    model = MiniCPMStreamingModel()
    duplex = model._duplex  # noqa: SLF001 — probe pokes the seam directly
    print(f"  model loaded in {round((time.monotonic() - t0) * 1000)}ms", flush=True)

    kokoro = _load_kokoro()
    user_audio = _synthesize_16k(kokoro, _USER_UTTERANCE)

    # Build the per-chunk audio timeline.
    buf = np.zeros(_TOTAL_CHUNKS * _CHUNK_SAMPLES, dtype=np.float32)
    if len(user_audio) > 0:
        end = min(len(user_audio), _USER_CHUNKS * _CHUNK_SAMPLES)
        buf[:end] = user_audio[:end]

    # Fresh session.
    duplex.model.reset_session(reset_token2wav_cache=False)  # type: ignore[attr-defined]
    duplex.prepare(prefix_system_prompt=_PROMPTED_SYSTEM_PROMPT)  # type: ignore[attr-defined]

    print("\nStepping duplex loop...\n", flush=True)
    header = f"{'chunk':<6} {'event':<14} {'is_listen':<10} {'text_preview'}"
    print(header)
    print("-" * 70)

    injection_ok = False
    injection_error: str | None = None
    spoke_post_injection = False
    trajectory: list[dict] = []

    for t in range(_TOTAL_CHUNKS):
        chunk = buf[t * _CHUNK_SAMPLES : (t + 1) * _CHUNK_SAMPLES]
        event = ""
        try:
            if t == _INJECT_CHUNK:
                event = "INJECT"
                pf = duplex.streaming_prefill(  # type: ignore[attr-defined]
                    audio_waveform=chunk, text_list=[_PENDING_NOTE]
                )
                injection_ok = bool(pf.get("success", False))
                if not injection_ok:
                    injection_error = f"streaming_prefill success=False: {pf.get('reason')}"
            else:
                duplex.streaming_prefill(audio_waveform=chunk)  # type: ignore[attr-defined]
            result = _generate(duplex)
        except Exception as exc:  # noqa: BLE001 — capture for verdict
            err = f"{type(exc).__name__}: {exc}"
            if t == _INJECT_CHUNK:
                injection_error = err
            print(f"{t:<6} {event:<14} {'ERROR':<10} {err}")
            trajectory.append({"t": t, "event": event, "error": err})
            print("\nVERDICT: NO-SHIP — exception during step (see above).")
            return 1

        is_listen = bool(result.get("is_listen", True))
        text = result.get("text", "")
        if t > _INJECT_CHUNK and not is_listen and text.strip():
            spoke_post_injection = True
        preview = text.replace("\n", " ")[:48]
        print(f"{t:<6} {event:<14} {str(is_listen):<10} {preview!r}")
        trajectory.append({"t": t, "event": event, "is_listen": is_listen, "text": text})

    out_path = "/tmp/probe_tact_pending_injection.json"
    with open(out_path, "w") as f:
        json.dump(
            {
                "injection_ok": injection_ok,
                "injection_error": injection_error,
                "spoke_post_injection": spoke_post_injection,
                "trajectory": trajectory,
            },
            f,
            indent=2,
        )
    print(f"\nRaw trajectory saved to {out_path}")

    print()
    if not injection_ok:
        print(f"VERDICT: NO-SHIP — mid-session text injection failed: {injection_error}")
        return 1
    obs = "spoke post-injection" if spoke_post_injection else (
        "did NOT speak post-injection (mechanically OK; behavior is what the runner measures)"
    )
    print(f"VERDICT: SHIP — streaming_prefill(text_list=...) injected mid-session; model {obs}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
