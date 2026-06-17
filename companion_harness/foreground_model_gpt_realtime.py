"""OpenAI `gpt-realtime-2` adapter for the TACT-Bench Layer-3 Mode-A experiment.

Implements the duck type the Layer-3 elicitation loop expects (`chat` for the text
arms, `chat_audio` for the audio arm) so the model-agnostic scorer/runner/reporter
run unchanged (see `experiments/gpt-realtime/execution-plan.md`).

`gpt-realtime-2` is a **Realtime-only** model: `/v1/chat/completions` rejects it
("not a chat model") and `/v1/responses` doesn't list it. It is reachable over the
GA Realtime **WebSocket** (`wss://api.openai.com/v1/realtime?model=gpt-realtime-2`,
no `OpenAI-Beta` header). Each TACT probe is one **out-of-band** `response.create`
(`conversation:"none"`, `output_modalities:["text"]`, inline `input`) — a stateless
request→text-decision, which is exactly the Mode-A per-tick probe. Time is
externalized into each probe by the harness ("second t + user state"); this adapter
never tracks elapsed time (Mode-A). One persistent WS session serves all probes,
driven from sync code via a background asyncio loop.

Determinism: Realtime exposes no `seed` and clamps temperature; we rely on repeated
runs + spread (plan §Determinism), not bit-identical decode.

Adapter-first (CLAUDE.md): the model SDK / transport is imported only here.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import threading
from dataclasses import dataclass, field

import numpy as np

_SAMPLE_RATE = 16000
_REALTIME_RATE = 24000  # Realtime input audio is PCM16 @ 24 kHz, mono
_WS_URL = "wss://api.openai.com/v1/realtime?model={model}"


def _pcm16_24k_b64(audio: np.ndarray, src_rate: int = _SAMPLE_RATE) -> str:
    """float32 [-1,1] @ src_rate -> 24 kHz PCM16 mono -> base64 (raw, no WAV container).

    Realtime `input_audio` content carries raw PCM16 at the session input rate (24k).
    """
    a = np.asarray(audio, dtype=np.float32)
    if src_rate != _REALTIME_RATE and len(a) > 1:
        n = int(round(len(a) * _REALTIME_RATE / src_rate))
        a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a).astype(np.float32)
    pcm16 = (a * 32767.0).clip(-32768, 32767).astype("<i2")
    return base64.b64encode(pcm16.tobytes()).decode("ascii")


def _text_msg(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _audio_msg(b64: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_audio", "audio": b64}]}


def _response_payload(input_msgs: list[dict], instructions: str, max_tokens: int) -> dict:
    """An out-of-band text response over a held session (does not touch the conversation)."""
    resp: dict = {
        "conversation": "none",
        "output_modalities": ["text"],
        "input": input_msgs,
        "max_output_tokens": max_tokens,
    }
    if instructions:
        resp["instructions"] = instructions
    return {"type": "response.create", "response": resp}


def _reduce_text(event: dict) -> tuple[str, bool, str | None]:
    """(text_delta, done, error) for one server event."""
    t = event.get("type", "")
    if t.endswith("output_text.delta"):
        return event.get("delta", ""), False, None
    if t == "response.done":
        return "", True, None
    if t == "error":
        return "", True, json.dumps(event.get("error", event))[:240]
    return "", False, None


class _RealtimeWsTransport:
    """Persistent GA Realtime WebSocket, driven from sync code via a background loop.

    `request(payload)` sends one `response.create` and blocks until `response.done`,
    returning the concatenated output text. Serialized (one in flight at a time), so
    out-of-band responses don't interleave. Reconnects once on a dropped socket.
    """

    def __init__(self, model: str, timeout: float = 120.0) -> None:
        self.model = model
        self.timeout = timeout
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._ws = None
        self._lock = threading.Lock()
        self._run(self._connect())

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=self.timeout)

    async def _connect(self) -> None:
        import websockets  # noqa: WPS433 — optional dep, lazy

        url = _WS_URL.format(model=self.model)
        headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}  # GA: no beta header
        self._ws = await websockets.connect(url, additional_headers=headers, max_size=None)
        await asyncio.wait_for(self._ws.recv(), self.timeout)  # session.created

    async def _request(self, payload: dict, retry: bool = True) -> str:
        import websockets  # noqa: WPS433

        try:
            await self._ws.send(json.dumps(payload))
            text = ""
            while True:
                ev = json.loads(await asyncio.wait_for(self._ws.recv(), self.timeout))
                delta, done, err = _reduce_text(ev)
                text += delta
                if err:
                    raise RuntimeError(f"realtime error: {err}")
                if done:
                    return text
        except (websockets.ConnectionClosed, asyncio.TimeoutError):
            if retry:
                await self._connect()
                return await self._request(payload, retry=False)
            raise

    def request(self, payload: dict) -> str:
        with self._lock:
            return self._run(self._request(payload))


@dataclass
class GptRealtimeModel:
    """`gpt-realtime-2` over the Realtime WebSocket, duck-typed like MiniCPMStreamingModel.

    - `chat(text)`        — text arms; the arm system prompt is already prepended to
                            `text` by `_probe_prompt`, sent as the user turn.
    - `chat_audio(audio, system_prompt)` — audio arm; system prompt as the response
                            `instructions`, the spoken probe as `input_audio`. Text out.

    `transport_factory` is injectable so tests run hermetically (no WebSocket).
    """

    model: str = "gpt-realtime-2"
    sample_rate: int = _SAMPLE_RATE
    # gpt-realtime-2 spends ~50–60 tokens internally before emitting visible text, so a
    # raw budget of 6 returns "" — add headroom to every caller's max_new_tokens.
    reasoning_headroom: int = 80
    transport_factory: object | None = None
    _transport: object | None = field(default=None, init=False, repr=False)

    def _t(self) -> object:
        if self._transport is None:
            self._transport = (
                self.transport_factory() if self.transport_factory is not None
                else _RealtimeWsTransport(self.model)
            )
        return self._transport

    def chat(self, text: str, max_new_tokens: int = 6) -> str:
        payload = _response_payload(
            [_text_msg(text)], instructions="", max_tokens=max_new_tokens + self.reasoning_headroom)
        return self._t().request(payload).strip()

    def chat_audio(self, audio: np.ndarray, system_prompt: str = "", max_new_tokens: int = 6) -> str:
        payload = _response_payload(
            [_audio_msg(_pcm16_24k_b64(audio, self.sample_rate))],
            instructions=system_prompt, max_tokens=max_new_tokens + self.reasoning_headroom,
        )
        return self._t().request(payload).strip()
