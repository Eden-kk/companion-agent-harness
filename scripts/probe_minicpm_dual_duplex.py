#!/raid/yid042/venvs/companion-harness/bin/python
"""Stage 0 probe: dual duplex sessions, generate_audio=True availability, prepare signature."""
from __future__ import annotations

import inspect
import json
import socket
import sys
import traceback
from datetime import datetime, timezone

import numpy as np
import torch
from transformers import AutoModel

_MODEL_ID = "openbmb/MiniCPM-o-4_5"
_SILENCE = np.zeros(16000, dtype=np.float32)

verdict: dict = {
    "load_mode": "unknown",
    "plan_b_engaged": None,
    "ref_audio_path": "unknown",
    "generate_audio_true_error": None,
    "audio_waveform_type": None,
    "prepare_signature": None,
    "timestamp_utc": datetime.now(timezone.utc).isoformat(),
    "host": socket.gethostname(),
}


def _run_ticks(duplex, n: int = 3) -> list[dict]:
    results = []
    for _ in range(n):
        duplex.streaming_prefill(audio_waveform=_SILENCE)
        r = duplex.streaming_generate(listen_prob_scale=1.0)
        results.append({
            "is_listen": bool(r.get("is_listen", True)),
            "text": r.get("text", ""),
            "end_of_turn": bool(r.get("end_of_turn", False)),
        })
    return results


print("Loading base model...", flush=True)
base = AutoModel.from_pretrained(
    _MODEL_ID,
    trust_remote_code=True,
    attn_implementation="sdpa",
    torch_dtype=torch.bfloat16,
    init_vision=False,
    init_audio=True,
    init_tts=True,
).eval().cuda()
print("Model loaded.", flush=True)

# --- Probe 0.a: dual duplex sessions ---
try:
    duplex_a = base.as_duplex(generate_audio=False, chunk_ms=1000)
    duplex_b = base.as_duplex(generate_audio=False, chunk_ms=1000)
    duplex_a.prepare(prefix_system_prompt="You are speaker A.")
    duplex_b.prepare(prefix_system_prompt="You are speaker B.")
    print("Running ticks for duplex_a...", flush=True)
    ticks_a = _run_ticks(duplex_a)
    print("Running ticks for duplex_b...", flush=True)
    ticks_b = _run_ticks(duplex_b)
    print(f"duplex_a ticks: {ticks_a}", flush=True)
    print(f"duplex_b ticks: {ticks_b}", flush=True)
    verdict["load_mode"] = "single"
    print("Probe 0.a: single-model dual-session OK", flush=True)
except Exception as exc:
    print(f"Probe 0.a single failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)
    # Fallback: try two separate loads
    try:
        print("Attempting dual load fallback...", flush=True)
        base2 = AutoModel.from_pretrained(
            _MODEL_ID,
            trust_remote_code=True,
            attn_implementation="sdpa",
            torch_dtype=torch.bfloat16,
            init_vision=False,
            init_audio=True,
            init_tts=True,
        ).eval().cuda()
        dx_a = base.as_duplex(generate_audio=False, chunk_ms=1000)
        dx_b = base2.as_duplex(generate_audio=False, chunk_ms=1000)
        dx_a.prepare(prefix_system_prompt="You are speaker A.")
        dx_b.prepare(prefix_system_prompt="You are speaker B.")
        _run_ticks(dx_a)
        _run_ticks(dx_b)
        verdict["load_mode"] = "dual"
        print("Probe 0.a: dual load OK", flush=True)
    except Exception as exc2:
        print(f"Probe 0.a dual fallback also failed: {type(exc2).__name__}: {exc2}", file=sys.stderr, flush=True)
        verdict["load_mode"] = "dual"  # conservative assumption

# --- Probe 0.b: generate_audio=True ---
try:
    dx_tts = base.as_duplex(generate_audio=True, chunk_ms=1000)
    dx_tts.prepare(prefix_system_prompt="Test.")
    dx_tts.streaming_prefill(audio_waveform=_SILENCE)
    r = dx_tts.streaming_generate(listen_prob_scale=1.0)
    wav = r.get("audio_waveform")
    if wav is None:
        verdict["audio_waveform_type"] = None
    elif isinstance(wav, np.ndarray):
        verdict["audio_waveform_type"] = "np.ndarray"
    elif isinstance(wav, torch.Tensor):
        verdict["audio_waveform_type"] = "torch.Tensor"
    elif isinstance(wav, bytes):
        verdict["audio_waveform_type"] = "bytes"
    else:
        verdict["audio_waveform_type"] = type(wav).__name__
    verdict["plan_b_engaged"] = False
    print(f"Probe 0.b: generate_audio=True OK; audio_waveform type={verdict['audio_waveform_type']}", flush=True)
except Exception as exc:
    msg = f"{type(exc).__name__}: {exc}"
    verdict["generate_audio_true_error"] = msg
    verdict["plan_b_engaged"] = True
    print(f"Probe 0.b: generate_audio=True FAILED → Plan B engaged. {msg}", file=sys.stderr, flush=True)

# --- Probe 0.c: ref_audio plumbing path ---
try:
    dx_sig = base.as_duplex(generate_audio=False, chunk_ms=1000)
    sig = inspect.signature(dx_sig.prepare)
    verdict["prepare_signature"] = str(sig)
    if "ref_audio" in sig.parameters:
        verdict["ref_audio_path"] = "prepare_kwarg"
    else:
        try:
            setattr(dx_sig, "ref_audio", None)
            if hasattr(dx_sig, "ref_audio"):
                verdict["ref_audio_path"] = "setattr"
        except Exception:
            verdict["ref_audio_path"] = "unknown"
    print(f"Probe 0.c: ref_audio_path={verdict['ref_audio_path']}, prepare sig={verdict['prepare_signature']}", flush=True)
except Exception as exc:
    print(f"Probe 0.c failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    traceback.print_exc(file=sys.stderr)

# Write verdict
out_path = "artifacts/stage0_verdict.json"
with open(out_path, "w") as f:
    json.dump(verdict, f, indent=2)
print(f"\nVerdict written to {out_path}", flush=True)
print(json.dumps(verdict, indent=2), flush=True)
