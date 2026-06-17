"""Validate MiniCPMStreamingModel.chat_audio (audio-in / text-out) before the
full TACT Layer-3 audio-arm run.

    PYTHONPATH=. /raid/yid042/venvs/companion-harness/bin/python scripts/probe_chat_audio.py

Loads MiniCPM-o once, then: (1) chat_audio on a synthetic tone, (2) chat_audio on
a Kokoro-rendered TACT probe with the prompted system prompt. Prints raw outputs +
the parsed NOW/WAIT/DROP token. Exits non-zero if the audio-content API errors.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion_harness.evals.adapters.tact_bench import _ARMS, _load_kokoro, _synthesize_16k  # noqa: E402
from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3  # noqa: E402
from companion_harness.evals.adapters.tact_bench_layer3_run import (  # noqa: E402
    _parse_token,
    _situational_probe,
    load_context,
)


def main() -> int:
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

    print("loading MiniCPM-o ...", flush=True)
    model = MiniCPMStreamingModel()
    print("loaded.", flush=True)

    # (1) synthetic tone — pure API smoke test
    tone = (0.1 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.float32)
    try:
        out = model.chat_audio(tone, system_prompt="You are a helpful assistant.", max_new_tokens=8)
        print(f"[tone] chat_audio OK -> {out!r}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[tone] chat_audio FAILED: {type(exc).__name__}: {exc}", flush=True)
        return 1

    # (2) a real TACT probe rendered via Kokoro
    kokoro = _load_kokoro()
    if kokoro is None:
        print("Kokoro not available — cannot validate the real audio probe.", flush=True)
        return 2
    cases = {c.id: c for c in load_layer3()}
    ctx = load_context()
    tc = cases["TC17"]
    item = tc.items[0]
    body = _situational_probe(tc, item, item.t_avail, ctx.get("TC17", {}))
    audio = _synthesize_16k(kokoro, body)
    print(f"[probe] tts -> {audio.shape[0]} samples ({audio.shape[0]/16000:.1f}s)", flush=True)
    raw = model.chat_audio(audio, system_prompt=_ARMS["audio"], max_new_tokens=6)
    print(f"[probe] item={item.id} raw={raw!r} parsed={_parse_token(raw)}", flush=True)
    print("PROBE OK", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
