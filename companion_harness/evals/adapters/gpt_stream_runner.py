"""gpt-realtime-2 continuous-session streaming runner for the TACT-Bench harness.

One persistent WebSocket session per run_case call.  User audio is streamed
in 1s PCM16@24k chunks (real-time paced when realtime=True); the held-item
note is injected via conversation.item.create(role=system) at t_avail; gpt is
given a response opportunity ONLY at oracle-floor seams (user_state b or i);
onset tick = the seam tick.

No live API calls in this file; the transport is injectable for hermetic tests.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import yaml

from companion_harness.evals.adapters.delivery_detector import annotate_emissions
from companion_harness.evals.adapters.scenario_timeline import (
    _CASES_DIR,
    _find_l3,
    _normalize_items,
    build_timeline,
)
from companion_harness.evals.adapters.tact_bench import _ARMS
from companion_harness.evals.adapters.tact_bench_layer3 import load_layer3
from companion_harness.evals.adapters.tact_bench_stream_types import Emission
from companion_harness.foreground_model_gpt_realtime import _pcm16_24k_b64

_REASONING_HEADROOM = 80  # §S0b finding: ≥80 tokens or output is empty

# Deliverable seam states: gpt gets a response opportunity ONLY at these ticks.
# 'm' (user speaking) and 'h' (held pause) are non-seams — no trigger.
# In all-'m' cases gpt never gets an opportunity → structural miss for urgent items
# (the honest reactive-vs-full-duplex contrast; §9 gpt cadence decision).
_SEAM_STATES = frozenset({"b", "i"})


# ---------------------------------------------------------------------------
# Continuous-session transport interface
# ---------------------------------------------------------------------------

class ContinuousTransport:
    """Async continuous-session transport (injectable for tests).

    Stateful: holds the WS session across append/inject/commit calls.
    """

    async def connect(self, instructions: str) -> None:
        raise NotImplementedError

    async def append_audio(self, b64: str) -> None:
        raise NotImplementedError

    async def inject_note(self, text: str) -> None:
        """Inject a system-role context note (conversation.item.create)."""
        raise NotImplementedError

    async def commit_and_respond(
        self, max_output_tokens: int
    ) -> tuple[str, str, float]:
        """Commit the audio buffer, request a response, drain to response.done.

        Returns (response_text, onset_event_type, onset_wall_ts).
        onset_wall_ts is time.time() at the first onset event.
        """
        raise NotImplementedError

    async def close(self) -> None:
        pass


class _RealtimeContinuousTransport(ContinuousTransport):
    """Live gpt-realtime-2 WS session.  Requires OPENAI_API_KEY env var."""

    _URL = "wss://api.openai.com/v1/realtime?model=gpt-realtime-2"

    def __init__(self) -> None:
        self._ws = None

    async def connect(self, instructions: str) -> None:
        import websockets  # noqa: WPS433

        headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
        self._ws = await websockets.connect(self._URL, additional_headers=headers, max_size=None)
        await asyncio.wait_for(self._ws.recv(), 30)  # session.created
        # session.update: arm instructions + disable server VAD (scripted commits only)
        await self._ws.send(json.dumps({
            "type": "session.update",
            "session": {
                "instructions": instructions,
                # VAD off: harness drives commit+response.create at seam ticks only.
                # GA shape confirmed via live smoke; if rejected, try {"type": "server_vad"}
                # and re-run smoke to determine the correct shape for this API version.
                "turn_detection": {"type": "none"},
                "output_modalities": ["text"],
            },
        }))
        # drain session.updated
        while True:
            ev = json.loads(await asyncio.wait_for(self._ws.recv(), 10))
            if ev.get("type") in ("session.updated", "error"):
                break

    async def append_audio(self, b64: str) -> None:
        await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": b64}))

    async def inject_note(self, text: str) -> None:
        await self._ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {"type": "message", "role": "system",
                     "content": [{"type": "input_text", "text": text}]},
        }))

    async def commit_and_respond(self, max_output_tokens: int) -> tuple[str, str, float]:
        await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await self._ws.send(json.dumps({
            "type": "response.create",
            "response": {"output_modalities": ["text"], "max_output_tokens": max_output_tokens},
        }))
        text = ""
        onset_type = ""
        onset_ts = 0.0
        _ONSET_TYPES = frozenset({
            "response.created",
            "response.output_text.delta",
            "response.output_audio_transcript.delta",
        })
        while True:
            ev = json.loads(await asyncio.wait_for(self._ws.recv(), 60))
            et = ev.get("type", "")
            if et in _ONSET_TYPES and not onset_type:
                onset_type = et
                onset_ts = time.time()
            if et.endswith("output_text.delta") or et.endswith("output_audio_transcript.delta"):
                text += ev.get("delta", "")
            elif et == "response.done":
                break
            elif et == "error":
                raise RuntimeError(json.dumps(ev.get("error", ev))[:240])
        return text.strip(), onset_type, onset_ts

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


# ---------------------------------------------------------------------------
# run_case
# ---------------------------------------------------------------------------

def run_case(
    case_id: str,
    arm: str,
    *,
    realtime: bool = True,
    tick_dur: float = 1.0,
    transport_factory: Callable[[], ContinuousTransport] | None = None,
    max_response_tokens: int = 40,
    k: int = 1,
) -> list[Emission]:
    """Stream *case_id* through gpt-realtime-2 and return annotated Emissions.

    Parameters
    ----------
    case_id
        Layer-3 case id, e.g. "TC1".
    arm
        "vanilla" or "prompted" (both are audio input for gpt; §4.5 freeze).
    realtime
        If True, sleep tick_dur between audio appends (live pacing).
        Set False for hermetic tests so they run instantly.
    tick_dur
        Duration of each tick in seconds (default 1.0).
    transport_factory
        Callable returning a ContinuousTransport; defaults to live WS transport.
    max_response_tokens
        Budget before headroom is added.
    k
        Number of independent runs; returns the last run's emissions (caller
        aggregates across k if needed; run_tact_stream handles repetition).
    """
    if arm not in _ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {sorted(_ARMS)}")

    instructions = _ARMS[arm]
    timeline = build_timeline(case_id)

    # Build pending_payloads for annotate_emissions
    l3 = yaml.safe_load((_CASES_DIR / "layer3-formal-trajectories.yaml").read_text())
    l3_case = _find_l3(l3["cases"], case_id)
    l2 = yaml.safe_load((_CASES_DIR / "layer2-semistructured.yaml").read_text())
    items = _normalize_items(l3_case, l2["scenarios"])
    pending_payloads: dict[str, str] = {it["id"]: str(it.get("payload", "")) for it in items}
    earlier_asks: dict[str, str] = {
        it["id"]: str(it["earlier_ask"])
        for it in items
        if it.get("earlier_ask")
    }

    # Per-tick floor from layer-3 user_state.  Seams are b/i; m/h are non-seams.
    l3_cases = load_layer3()
    l3_obj = next((c for c in l3_cases if c.id == case_id), None)
    user_state: list[str] = l3_obj.user_state if l3_obj is not None else ["i"] * len(timeline.ticks)

    factory = transport_factory or _RealtimeContinuousTransport

    last_emissions: list[Emission] = []
    for _ in range(k):
        last_emissions = asyncio.run(
            _run_once(
                timeline=timeline,
                l3_case=l3_case,
                items=items,
                pending_payloads=pending_payloads,
                earlier_asks=earlier_asks,
                instructions=instructions,
                realtime=realtime,
                tick_dur=tick_dur,
                transport_factory=factory,
                max_response_tokens=max_response_tokens,
                user_state=user_state,
            )
        )
    return last_emissions


async def _run_once(
    *,
    timeline,
    l3_case: dict,
    items: list[dict],
    pending_payloads: dict[str, str],
    earlier_asks: dict[str, str],
    instructions: str,
    realtime: bool,
    tick_dur: float,
    transport_factory,
    max_response_tokens: int,
    user_state: list[str],
) -> list[Emission]:
    transport = transport_factory()
    await transport.connect(instructions)

    audio_chunks = _build_audio_chunks(timeline)
    raw: list[tuple[int, bool, str]] = []
    max_tok = max_response_tokens + _REASONING_HEADROOM

    try:
        for tick in timeline.ticks:
            t = tick.t

            # inject pending note BEFORE seam handling
            if tick.pending_note is not None:
                await transport.inject_note(tick.pending_note)

            await transport.append_audio(audio_chunks[t])

            if realtime:
                await asyncio.sleep(tick_dur)

            # Oracle-floor-timed: trigger only at deliverable seams (b or i) AND
            # only once at least one item is available — no spurious triggers before
            # any item exists (e.g. TC4 idle ticks before t_avail).
            floor = user_state[t] if t < len(user_state) else "i"
            if floor not in _SEAM_STATES:
                continue
            if not any(it["t_avail"] <= t for it in items):
                continue

            try:
                resp_text, onset_evt, onset_ts = await transport.commit_and_respond(max_tok)
            except Exception:  # noqa: BLE001
                resp_text, onset_evt, onset_ts = "", "", 0.0

            if resp_text:
                # onset tick = the seam tick (harness-driven commit; onset_ts not used for tick)
                raw.append((t, True, resp_text))
    finally:
        await transport.close()

    return annotate_emissions(raw, l3_case, pending_payloads, earlier_asks)


def _build_audio_chunks(timeline) -> list[str]:
    """Return per-tick base64 PCM16@24k chunks.

    Uses audio_view() when Kokoro is available; falls back to 1s silence
    so the runner is usable in test/CI without TTS.
    """
    try:
        pcm_list = timeline.audio_view()
        return [_pcm16_24k_b64(pcm, src_rate=16000) for pcm in pcm_list]
    except RuntimeError:
        silence = _pcm16_24k_b64(np.zeros(16000, dtype=np.float32), src_rate=16000)
        return [silence] * len(timeline.ticks)
