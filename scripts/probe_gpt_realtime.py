"""Pre-flight for the gpt-realtime-2 TACT audio/text arms (SP2).

    OPENAI_API_KEY=... PYTHONPATH=. python scripts/probe_gpt_realtime.py

Validates, with a few cheap real calls:
  1. text-in -> text-out on gpt-realtime-2 via Chat Completions (a real TACT probe);
  2. that temperature=0 / seed / modalities are accepted (else reports a working config);
  3. audio-in -> text-out (Kokoro-TTS'd probe).
Prints raw output + the parsed NOW/WAIT/DROP token. Passing = a valid token back.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion_harness.evals.adapters.tact_bench import _ARMS, _load_kokoro, _synthesize_16k  # noqa: E402
from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3  # noqa: E402
from companion_harness.evals.adapters.tact_bench_layer3_run import (  # noqa: E402
    _parse_token,
    _probe_prompt,
    _situational_probe,
    load_context,
)
from companion_harness.foreground_model_gpt_realtime import GptRealtimeModel  # noqa: E402

_VALID = {"WAIT", "DROP", "NOW:SPEAK_BRIEF", "NOW:SPEAK_FULL", "NOW:SILENT_NOTIFY", "NOW:CHIME"}


def _try_text(text: str):
    """Return (model, raw) for the first config that succeeds; raise the last error."""
    configs = [
        dict(),                                   # temperature=0, seed=7, modalities=("text",)
        dict(modalities=None),                    # drop modalities
        dict(temperature=1.0),                    # some realtime models clamp temp>=0.6
        dict(temperature=1.0, modalities=None),
    ]
    last = None
    for cfg in configs:
        try:
            m = GptRealtimeModel(**cfg)
            raw = m.chat(text, max_new_tokens=6)
            print(f"  [text] config OK: {cfg or 'defaults'}", flush=True)
            return m, raw
        except Exception as exc:  # noqa: BLE001
            print(f"  [text] config {cfg or 'defaults'} FAILED: {type(exc).__name__}: {exc}", flush=True)
            last = exc
    raise last  # type: ignore[misc]


def main() -> int:
    cases = {c.id: c for c in load_layer3()}
    ctx = load_context()
    tc = cases["TC3"]            # meeting: NOW:SPEAK_BRIEF+INT @ t6
    item = tc.items[0]
    text_probe = _probe_prompt(tc, item, item.t_avail, _ARMS["prompted"], ctx.get("TC3", {}))

    print("== text smoke ==", flush=True)
    model, raw = _try_text(text_probe)
    parsed = _parse_token(raw)
    print(f"  raw={raw!r}  parsed={parsed}  valid={parsed in _VALID}", flush=True)

    print("== audio smoke ==", flush=True)
    kokoro = _load_kokoro()
    if kokoro is None:
        print("  Kokoro unavailable — set KOKORO_MODEL_PATH/KOKORO_VOICES_PATH. Audio arm BLOCKED.", flush=True)
        return 2
    body = _situational_probe(tc, item, item.t_avail, ctx.get("TC3", {}))
    audio = _synthesize_16k(kokoro, body)
    print(f"  tts -> {audio.shape[0]} samples ({audio.shape[0]/16000:.1f}s)", flush=True)
    raw_a = model.chat_audio(audio, system_prompt=_ARMS["audio"], max_new_tokens=6)
    parsed_a = _parse_token(raw_a)
    print(f"  raw={raw_a!r}  parsed={parsed_a}  valid={parsed_a in _VALID}", flush=True)

    ok = (parsed in _VALID) and (parsed_a in _VALID)
    print("PROBE OK" if ok else "PROBE: token parse weak — tighten the answer instruction", flush=True)
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
