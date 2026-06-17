"""S0b probe — gpt-realtime-2 CONTINUOUS real-time session round-trip.

Proves the net-new mechanic the streaming-trajectory rewrite needs (the current
adapter is stateless out-of-band only): one persistent WS session that takes
MULTIPLE user inputs over time, accepts a held-result note injected mid-session,
and lets the model reference it — and that streamed input_audio drives a response
whose onset we can timestamp (the "spoke this tick" signal).

Run (key injected inline at runtime, never stored):
    env OPENAI_API_KEY=sk-... /raid/yid042/venvs/companion-harness/bin/python \
        scripts/probe_gpt_realtime_session.py

Reads OPENAI_API_KEY from the environment only; never hardcodes/prints/commits it.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from companion_harness.foreground_model_gpt_realtime import _pcm16_24k_b64  # noqa: E402

_URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2"
_TOKEN = "2.4.1"  # benign TACT-like payload token (avoid 'secret' phrasing -> privacy refusal)
_NOTE = f"[PENDING - from async_task: the deploy finished successfully, build {_TOKEN}]"

_T0 = time.time()


def _ts() -> str:
    return f"{time.time() - _T0:6.2f}s"


async def _send(ws, obj: dict) -> None:
    await ws.send(json.dumps(obj))
    print(f"  {_ts()}  -> {obj['type']}", flush=True)


async def _drain_response(ws, timeout: float = 30.0) -> tuple[str, list[str]]:
    """Read events until response.done/error. Return (text, event_types_seen)."""
    text = ""
    seen: list[str] = []
    first_delta_logged = False
    while True:
        ev = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        et = ev.get("type", "?")
        seen.append(et)
        if et.endswith("output_text.delta") or et.endswith("output_audio_transcript.delta"):
            if not first_delta_logged:
                print(f"  {_ts()}  <- FIRST OUTPUT DELTA ({et})", flush=True)
                first_delta_logged = True
            text += ev.get("delta", "")
        elif et == "response.done":
            print(f"  {_ts()}  <- response.done", flush=True)
            return text, seen
        elif et == "error":
            print(f"  {_ts()}  <- ERROR: {json.dumps(ev.get('error', ev))[:300]}", flush=True)
            return text, seen


async def _tts_24k_chunks(line: str) -> list[str]:
    """Kokoro TTS -> list of 1s base64 PCM16@24k chunks (empty list if TTS absent)."""
    try:
        from companion_harness.evals.adapters.tact_bench import _load_kokoro, _synthesize_16k
        kokoro = _load_kokoro()
        if kokoro is None:
            return []
        audio16 = _synthesize_16k(kokoro, line)  # float32 @16k
    except Exception as exc:  # noqa: BLE001
        print(f"  (TTS unavailable: {type(exc).__name__}: {exc}; using 1s silence)", flush=True)
        audio16 = np.zeros(16000, dtype=np.float32)
    chunks = []
    for i in range(0, max(len(audio16), 1), 16000):
        seg = audio16[i:i + 16000]
        if len(seg) == 0:
            break
        chunks.append(_pcm16_24k_b64(seg, src_rate=16000))
    return chunks


async def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY MISSING — aborting (env-only).", flush=True)
        return 2
    import websockets  # noqa: WPS433

    headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
    print(f"== connecting {_URL} ==", flush=True)
    async with websockets.connect(_URL, additional_headers=headers, max_size=None) as ws:
        created = json.loads(await asyncio.wait_for(ws.recv(), 30))
        print(f"  {_ts()}  <- {created.get('type')}", flush=True)

        # (Rely on default session config; server VAD default is fine for the probe.)

        # ---- Phase A: multi-turn TEXT input + mid-session injection + reference ----
        print("== Phase A: continuous multi-turn text + mid-stream injection ==", flush=True)
        ok_a = False
        try:
            # user turn 1
            await _send(ws, {"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Hey, I'm just planning my weekend, nothing urgent."}]}})
            # held-result note injected MID-SESSION (out of band from user speech)
            for role in ("system", "developer"):
                try:
                    await _send(ws, {"type": "conversation.item.create", "item": {
                        "type": "message", "role": role,
                        "content": [{"type": "input_text", "text": _NOTE}]}})
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"  inject role={role} failed: {exc}", flush=True)
            # user turn 2 (a second request, referencing the held note)
            await _send(ws, {"type": "conversation.item.create", "item": {
                "type": "message", "role": "user",
                "content": [{"type": "input_text",
                             "text": "Quick check - do you have any pending update you've been holding for me? If so, what is it?"}]}})
            await _send(ws, {"type": "response.create",
                             "response": {"output_modalities": ["text"], "max_output_tokens": 200}})
            text_a, seen_a = await _drain_response(ws)
            ok_a = _TOKEN in text_a or "deploy" in text_a.lower()
            print(f"  {_ts()}  response.text={text_a!r}", flush=True)
            print(f"  references the held note ({_TOKEN}/deploy)? {'YES' if ok_a else 'NO'}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  Phase A FAILED: {type(exc).__name__}: {exc}", flush=True)

        # ---- Phase B: streamed input_audio -> commit -> response (onset timing) ----
        print("== Phase B: streamed audio append/commit + response onset ==", flush=True)
        ok_b = False
        try:
            chunks = await _tts_24k_chunks("Hey there, can you hear me okay on this line?")
            print(f"  streaming {len(chunks)} x 1s audio chunks (real-time paced)...", flush=True)
            for b64 in chunks:
                await _send(ws, {"type": "input_audio_buffer.append", "audio": b64})
                await asyncio.sleep(1.0)  # real-time 1x pacing
            await _send(ws, {"type": "input_audio_buffer.commit"})
            await _send(ws, {"type": "response.create", "response": {"output_modalities": ["text"]}})
            text_b, seen_b = await _drain_response(ws)
            ok_b = len(text_b.strip()) > 0
            print(f"  {_ts()}  audio-response.text={text_b!r}", flush=True)
            print(f"  onset-signalling events seen: "
                  f"{sorted({e for e in seen_b if 'response' in e or 'audio' in e})}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  Phase B FAILED: {type(exc).__name__}: {exc}", flush=True)

    print(f"\nRESULT: phaseA(multi-turn+inject)={'OK' if ok_a else 'FAIL'}  "
          f"phaseB(audio-stream)={'OK' if ok_b else 'FAIL'}", flush=True)
    return 0 if (ok_a and ok_b) else 3


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
