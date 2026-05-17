#!/usr/bin/env python3
"""Post-deploy API test runner for the live console.

Usage:
    python tests/manual/api_test_runner.py --plan 1
    python tests/manual/api_test_runner.py --plan 1 --test 1.2
    python tests/manual/api_test_runner.py --plan 2 --test 2.3
    python tests/manual/api_test_runner.py --plan 3 --test 3.3

The console must already be running.  If /healthz is not 200 the script exits
with code 2.

Exit codes:
    0 — all assertions passed (skipped tests do not count as failures)
    1 — one or more assertions failed
    2 — server unreachable or /healthz not 200
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import math
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INGEST_SAMPLE_RATE = 16000
CHUNK_MS = 30
CHUNK_SAMPLES = INGEST_SAMPLE_RATE * CHUNK_MS // 1000   # 480 samples
CHUNK_BYTES = CHUNK_SAMPLES * 2                          # int16

# Trailing silence after speech so VAD/EOU fires.
TRAILING_SILENCE_MS = 800

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class TestResult:
    name: str
    passed: bool
    skipped: bool = False
    message: str = ""


@dataclass
class RunState:
    display_events: list[dict[str, Any]] = field(default_factory=list)
    audio_out_chunks: list[dict[str, Any]] = field(default_factory=list)
    capture_started: float = 0.0

# ---------------------------------------------------------------------------
# Audio fixture generators
# ---------------------------------------------------------------------------


def _silence_pcm16(duration_ms: int) -> bytes:
    n = INGEST_SAMPLE_RATE * duration_ms // 1000
    return b"\x00" * (n * 2)


def _speech_like_pcm16(duration_ms: int) -> bytes:
    """Multi-frequency sine sum that reliably triggers VAD above onset threshold.

    Recipe mirrors tests/test_vad_silero.py::test_silero_produces_speech_
    probability_above_zero_on_speech_like_audio.
    """
    n = INGEST_SAMPLE_RATE * duration_ms // 1000
    freqs = [200, 400, 600, 800]
    amplitude = 0.3
    samples: list[int] = []
    for i in range(n):
        t = i / INGEST_SAMPLE_RATE
        v = sum(math.sin(2 * math.pi * f * t) for f in freqs) * amplitude / len(freqs)
        samples.append(max(-32768, min(32767, int(v * 32767))))
    return struct.pack(f"<{n}h", *samples)


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


async def _check_health(session: aiohttp.ClientSession, base_url: str) -> dict[str, Any]:
    async with session.get(f"{base_url}/healthz") as resp:
        if resp.status != 200:
            raise RuntimeError(f"/healthz returned {resp.status}")
        return await resp.json()


async def _capture_display(
    session: aiohttp.ClientSession,
    ws_base: str,
    sink: list[dict[str, Any]],
    started_ref: list[float],
    stop_event: asyncio.Event,
) -> None:
    async with session.ws_connect(
        f"{ws_base}/ws/display", max_msg_size=8 * 1024 * 1024
    ) as ws:
        started_ref.append(time.monotonic())
        async for msg in ws:
            if stop_event.is_set():
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    sink.append(json.loads(msg.data))
                except json.JSONDecodeError:
                    pass
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break


async def _capture_audio_out(
    session: aiohttp.ClientSession,
    ws_base: str,
    sink: list[dict[str, Any]],
    stop_event: asyncio.Event,
) -> None:
    async with session.ws_connect(f"{ws_base}/ws/audio_out") as ws:
        async for msg in ws:
            if stop_event.is_set():
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    sink.append(json.loads(msg.data))
                except json.JSONDecodeError:
                    pass
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break


async def _stream_audio(
    ws: aiohttp.ClientWebSocketResponse,
    pcm16_bytes: bytes,
    client_id: str,
    trailing_silence_ms: int = TRAILING_SILENCE_MS,
) -> None:
    t0 = time.monotonic()
    sent = 0
    chunk_idx = 0
    while sent + CHUNK_BYTES <= len(pcm16_bytes):
        chunk = pcm16_bytes[sent : sent + CHUNK_BYTES]
        sent += CHUNK_BYTES
        envelope = {
            "event_type": "raw_audio",
            "payload_inline_or_ref": base64.b64encode(chunk).decode("ascii"),
            "timestamp_mono_ms": int((time.monotonic() - t0) * 1000),
            "client_id": client_id,
            "device_label": "api_test_runner",
        }
        await ws.send_json(envelope)
        chunk_idx += 1
        target_t = t0 + (chunk_idx * CHUNK_MS) / 1000.0
        delay = target_t - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    silence = b"\x00" * CHUNK_BYTES
    for _ in range(trailing_silence_ms // CHUNK_MS):
        envelope = {
            "event_type": "raw_audio",
            "payload_inline_or_ref": base64.b64encode(silence).decode("ascii"),
            "timestamp_mono_ms": int((time.monotonic() - t0) * 1000),
            "client_id": client_id,
            "device_label": "api_test_runner",
        }
        await ws.send_json(envelope)
        await asyncio.sleep(CHUNK_MS / 1000.0)


async def _wait_for_event_type(
    sink: list[dict[str, Any]],
    event_type: str,
    timeout_s: float,
    after_index: int = 0,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for evt in sink[after_index:]:
            if evt.get("event_type") == event_type:
                return evt
        await asyncio.sleep(0.1)
    return None


async def _wait_for_n_events(
    sink: list[dict[str, Any]],
    event_type: str,
    n: int,
    timeout_s: float,
    after_index: int = 0,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        matches = [e for e in sink[after_index:] if e.get("event_type") == event_type]
        if len(matches) >= n:
            return matches
        await asyncio.sleep(0.1)
    return [e for e in sink[after_index:] if e.get("event_type") == event_type]

# ---------------------------------------------------------------------------
# Plan 1 tests (Chinese-ready stack)
# ---------------------------------------------------------------------------


async def _test_1_1(http: aiohttp.ClientSession, base_url: str) -> TestResult:
    name = "1.1 — /healthz: ASR loaded"
    try:
        h = await _check_health(http, base_url)
    except Exception as exc:
        return TestResult(name, False, message=str(exc))
    if not h.get("asr_ready"):
        return TestResult(name, False, message=f"asr_ready={h.get('asr_ready')!r}, asr_model={h.get('asr_model')!r}")
    asr_model = h.get("asr_model", "")
    if str(asr_model).startswith("stub:"):
        return TestResult(name, False, message=f"asr_model is stub: {asr_model!r}")
    return TestResult(name, True, message=f"asr_model={asr_model!r}")


async def _test_1_2(
    http: aiohttp.ClientSession, base_url: str, ws_base: str
) -> TestResult:
    name = "1.2 — English audio → asr_transcript_emitted"
    events: list[dict[str, Any]] = []
    stop = asyncio.Event()
    started_ref: list[float] = []

    capture_task = asyncio.create_task(
        _capture_display(http, ws_base, events, started_ref, stop)
    )
    # Wait for display WS to connect.
    for _ in range(50):
        if started_ref:
            break
        await asyncio.sleep(0.1)

    snapshot_idx = len(events)
    audio = _speech_like_pcm16(2000)

    try:
        async with http.ws_connect(f"{ws_base}/ws/ingest") as ws:
            await _stream_audio(ws, audio, client_id="test-1-2")

        evt = await _wait_for_event_type(events, "asr_transcript_emitted", timeout_s=10, after_index=snapshot_idx)
    finally:
        stop.set()
        capture_task.cancel()
        try:
            await capture_task
        except asyncio.CancelledError:
            pass

    if evt is None:
        return TestResult(name, False, message="asr_transcript_emitted did not fire within 10 s")
    payload = evt.get("payload_inline") or {}
    transcript = payload.get("transcript", "")
    if isinstance(transcript, dict):
        transcript = transcript.get("value", "")
    if not transcript:
        return TestResult(name, False, message="transcript is empty")
    return TestResult(name, True, message=f"transcript length={len(str(transcript))}")


async def _test_1_3(
    http: aiohttp.ClientSession, base_url: str, ws_base: str
) -> TestResult:
    name = "1.3 — Chinese audio → ASR does not crash"
    # Use speech-like fixture as proxy for Chinese audio.
    # Operator should substitute a real Chinese WAV for full validation.
    events: list[dict[str, Any]] = []
    stop = asyncio.Event()
    started_ref: list[float] = []

    capture_task = asyncio.create_task(
        _capture_display(http, ws_base, events, started_ref, stop)
    )
    for _ in range(50):
        if started_ref:
            break
        await asyncio.sleep(0.1)

    snapshot_idx = len(events)
    audio = _speech_like_pcm16(2000)

    try:
        async with http.ws_connect(f"{ws_base}/ws/ingest") as ws:
            await _stream_audio(ws, audio, client_id="test-1-3")

        await asyncio.sleep(2.0)
    finally:
        stop.set()
        capture_task.cancel()
        try:
            await capture_task
        except asyncio.CancelledError:
            pass

    errors = [
        e for e in events[snapshot_idx:]
        if e.get("event_type") in ("tts_adapter_error", "policy_decision_error")
    ]
    if errors:
        return TestResult(name, False, message=f"error events: {[e['event_type'] for e in errors]}")

    try:
        h = await _check_health(http, base_url)
    except Exception as exc:
        return TestResult(name, False, message=f"/healthz failed after ingestion: {exc}")

    if h.get("status") != "ok":
        return TestResult(name, False, message=f"server status={h.get('status')!r} after ingestion")

    transcripts = [e for e in events[snapshot_idx:] if e.get("event_type") == "asr_transcript_emitted"]
    note = f"no errors; {len(transcripts)} transcript(s) (fixture is synthetic, not real Chinese)"
    return TestResult(name, True, message=note)


def _test_1_4_skip() -> TestResult:
    return TestResult(
        "1.4 — Chinese backchannel score >= 0.5",
        passed=False,
        skipped=True,
        message="TODO: requires backchannel model wired for Chinese",
    )


# ---------------------------------------------------------------------------
# Plan 3 tests (Tier-2 latency)
# ---------------------------------------------------------------------------


async def _test_3_1(http: aiohttp.ClientSession, base_url: str) -> TestResult:
    name = "3.1 — minicpm_loaded within 60 s"
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            h = await _check_health(http, base_url)
        except Exception as exc:
            return TestResult(name, False, message=str(exc))
        if h.get("minicpm_loaded") and h.get("foreground_model_ready"):
            return TestResult(name, True, message="minicpm_loaded=true, foreground_model_ready=true")
        await asyncio.sleep(2.0)
    return TestResult(name, False, message="minicpm_loaded still false after 60 s")


def _test_3_2_skip() -> TestResult:
    return TestResult(
        "3.2 — boot log contains warmup line",
        passed=False,
        skipped=True,
        message="Not assertable via HTTP — check process stdout/stderr for 'warmup: minicpm='",
    )


async def _test_3_3(
    http: aiohttp.ClientSession, base_url: str, ws_base: str
) -> TestResult:
    name = "3.3 — EOU → tts_synthesis_started < 5000 ms"
    events: list[dict[str, Any]] = []
    stop = asyncio.Event()
    started_ref: list[float] = []

    capture_task = asyncio.create_task(
        _capture_display(http, ws_base, events, started_ref, stop)
    )
    for _ in range(50):
        if started_ref:
            break
        await asyncio.sleep(0.1)

    snapshot_idx = len(events)
    audio = _speech_like_pcm16(3000)

    try:
        async with http.ws_connect(f"{ws_base}/ws/ingest") as ws:
            await _stream_audio(ws, audio, client_id="test-3-3")

        # Wait for vad_turn_signal then tts_synthesis_started.
        eou_evt = await _wait_for_event_type(events, "vad_turn_signal", timeout_s=10, after_index=snapshot_idx)
        if eou_evt is None:
            eou_evt = await _wait_for_event_type(events, "policy_decision", timeout_s=10, after_index=snapshot_idx)
        eou_idx = events.index(eou_evt) if eou_evt is not None else snapshot_idx

        tts_evt = await _wait_for_event_type(events, "tts_synthesis_started", timeout_s=8, after_index=eou_idx)
    finally:
        stop.set()
        capture_task.cancel()
        try:
            await capture_task
        except asyncio.CancelledError:
            pass

    if eou_evt is None:
        return TestResult(name, False, message="vad_turn_signal/policy_decision did not fire")
    if tts_evt is None:
        return TestResult(name, False, message="tts_synthesis_started did not fire within 8 s of EOU")

    eou_ms = eou_evt.get("timestamp_mono_ms", 0)
    tts_ms = tts_evt.get("timestamp_mono_ms", 0)
    delta_ms = tts_ms - eou_ms
    if delta_ms > 5000:
        return TestResult(name, False, message=f"EOU→tts_synthesis_started = {delta_ms} ms > 5000 ms")
    return TestResult(name, True, message=f"EOU→tts_synthesis_started = {delta_ms} ms")


def _test_3_4_skip() -> TestResult:
    return TestResult(
        "3.4 — probe_torch_compile_warmup verdict is SHIP",
        passed=False,
        skipped=True,
        message="Run scripts/probe_torch_compile_warmup.py separately and grep stdout for SHIP",
    )


# ---------------------------------------------------------------------------
# Plan 2 tests (Native MiniCPM TTS)
# ---------------------------------------------------------------------------


async def _test_2_1(http: aiohttp.ClientSession, base_url: str) -> TestResult:
    name = "2.1 — /healthz: native MiniCPM TTS loaded"
    try:
        h = await _check_health(http, base_url)
    except Exception as exc:
        return TestResult(name, False, message=str(exc))
    tts_model = h.get("tts_model", "")
    if tts_model != "MiniCPM-o native TTS":
        return TestResult(name, False, message=f"tts_model={tts_model!r} (expected 'MiniCPM-o native TTS')")
    if not h.get("tts_ready"):
        return TestResult(name, False, message="tts_ready=false")
    return TestResult(name, True, message=f"tts_model={tts_model!r}")


async def _test_2_2(
    http: aiohttp.ClientSession, base_url: str, ws_base: str
) -> TestResult:
    name = "2.2 — utterance produces tts_synthesis_started/completed + audio_buffer_queued"
    events: list[dict[str, Any]] = []
    stop = asyncio.Event()
    started_ref: list[float] = []

    capture_task = asyncio.create_task(
        _capture_display(http, ws_base, events, started_ref, stop)
    )
    for _ in range(50):
        if started_ref:
            break
        await asyncio.sleep(0.1)

    snapshot_idx = len(events)
    audio = _speech_like_pcm16(2000)

    try:
        async with http.ws_connect(f"{ws_base}/ws/ingest") as ws:
            await _stream_audio(ws, audio, client_id="test-2-2")

        started = await _wait_for_event_type(events, "tts_synthesis_started", timeout_s=20, after_index=snapshot_idx)
        if started is not None:
            started_idx = events.index(started)
        else:
            started_idx = snapshot_idx

        completed = await _wait_for_event_type(events, "tts_synthesis_completed", timeout_s=15, after_index=started_idx)
        buffered = await _wait_for_n_events(events, "assistant_audio_buffer_queued", 1, timeout_s=15, after_index=snapshot_idx)
    finally:
        stop.set()
        capture_task.cancel()
        try:
            await capture_task
        except asyncio.CancelledError:
            pass

    failures = []
    if started is None:
        failures.append("tts_synthesis_started missing")
    if completed is None:
        failures.append("tts_synthesis_completed missing")
    if not buffered:
        failures.append("assistant_audio_buffer_queued missing")
    if failures:
        return TestResult(name, False, message="; ".join(failures))
    return TestResult(name, True, message=f"audio_buffer_queued count={len(buffered)}")


async def _test_2_3(
    http: aiohttp.ClientSession, base_url: str, ws_base: str
) -> TestResult:
    name = "2.3 — first audio chunk first 200 ms RMS > 0.01 (CN7-FIX)"
    audio_chunks: list[dict[str, Any]] = []
    display_events: list[dict[str, Any]] = []
    stop = asyncio.Event()
    started_ref: list[float] = []

    display_task = asyncio.create_task(
        _capture_display(http, ws_base, display_events, started_ref, stop)
    )
    audio_task = asyncio.create_task(
        _capture_audio_out(http, ws_base, audio_chunks, stop)
    )
    for _ in range(50):
        if started_ref:
            break
        await asyncio.sleep(0.1)

    snapshot_idx = len(audio_chunks)
    audio = _speech_like_pcm16(2000)

    try:
        async with http.ws_connect(f"{ws_base}/ws/ingest") as ws:
            await _stream_audio(ws, audio, client_id="test-2-3")

        # Wait for at least one audio_out chunk.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and len(audio_chunks) <= snapshot_idx:
            await asyncio.sleep(0.1)
    finally:
        stop.set()
        display_task.cancel()
        audio_task.cancel()
        for t in (display_task, audio_task):
            try:
                await t
            except asyncio.CancelledError:
                pass

    new_chunks = audio_chunks[snapshot_idx:]
    if not new_chunks:
        return TestResult(name, False, message="no audio_out chunks received within 20 s")

    first = new_chunks[0]
    pcm_b64 = first.get("pcm_bytes_b64", "")
    if not pcm_b64:
        return TestResult(name, False, message="first chunk has no pcm_bytes_b64")

    raw = base64.b64decode(pcm_b64)
    # First 200 ms at 24 kHz = 4800 samples = 9600 bytes.
    sample_rate = int(first.get("sample_rate", 24000))
    window_samples = sample_rate * 200 // 1000
    window_bytes = window_samples * 2
    raw_window = raw[:window_bytes]
    if len(raw_window) < 2:
        return TestResult(name, False, message="first chunk too short to compute RMS")

    n = len(raw_window) // 2
    samples = struct.unpack(f"<{n}h", raw_window[:n * 2])
    rms = math.sqrt(sum(s * s for s in samples) / n) / 32768.0
    if rms <= 0.01:
        return TestResult(name, False, message=f"RMS={rms:.4f} <= 0.01 (leading silence detected)")
    return TestResult(name, True, message=f"RMS={rms:.4f}")


async def _test_2_4(
    http: aiohttp.ClientSession, base_url: str, ws_base: str
) -> TestResult:
    name = "2.4 — second utterance → policy_decision fires (B1-FIX)"
    events: list[dict[str, Any]] = []
    stop = asyncio.Event()
    started_ref: list[float] = []

    capture_task = asyncio.create_task(
        _capture_display(http, ws_base, events, started_ref, stop)
    )
    for _ in range(50):
        if started_ref:
            break
        await asyncio.sleep(0.1)

    audio = _speech_like_pcm16(2000)

    try:
        # First utterance — wait for TTS to start.
        async with http.ws_connect(f"{ws_base}/ws/ingest") as ws:
            await _stream_audio(ws, audio, client_id="test-2-4-turn1")

        await _wait_for_event_type(events, "tts_synthesis_started", timeout_s=15)
        snapshot_idx = len(events)

        # Small gap then second utterance.
        await asyncio.sleep(0.5)

        async with http.ws_connect(f"{ws_base}/ws/ingest") as ws:
            await _stream_audio(ws, audio, client_id="test-2-4-turn2")

        policy_evt = await _wait_for_event_type(
            events, "policy_decision", timeout_s=15, after_index=snapshot_idx
        )
    finally:
        stop.set()
        capture_task.cancel()
        try:
            await capture_task
        except asyncio.CancelledError:
            pass

    if policy_evt is None:
        return TestResult(name, False, message="policy_decision did not fire for second utterance within 15 s")
    action = (policy_evt.get("payload_inline") or {}).get("action_type", "?")
    return TestResult(name, True, message=f"policy_decision action_type={action!r}")


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

PLAN_TESTS: dict[int, dict[str, Any]] = {
    1: {
        "1.1": _test_1_1,
        "1.2": _test_1_2,
        "1.3": _test_1_3,
        "1.4": _test_1_4_skip,
    },
    2: {
        "2.1": _test_2_1,
        "2.2": _test_2_2,
        "2.3": _test_2_3,
        "2.4": _test_2_4,
    },
    3: {
        "3.1": _test_3_1,
        "3.2": _test_3_2_skip,
        "3.3": _test_3_3,
        "3.4": _test_3_4_skip,
    },
}

# Tests that need only (http, base_url) — no WS ingest.
_HTTP_ONLY = {"1.1", "2.1", "3.1", "3.2", "3.4", "1.4"}

# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


async def _run(
    plan: int,
    test_id: str | None,
    base_url: str,
    out_dir: Path | None,
) -> int:
    ws_base = base_url.replace("http://", "ws://").replace("https://", "wss://")

    async with aiohttp.ClientSession() as http:
        # Preflight health check.
        try:
            h = await _check_health(http, base_url)
        except Exception as exc:
            print(f"[PREFLIGHT FAIL] {exc}", flush=True)
            return 2
        print(f"[preflight] /healthz ok — mode={h.get('mode')!r} minicpm_loaded={h.get('minicpm_loaded')}", flush=True)

        tests = PLAN_TESTS.get(plan, {})
        if not tests:
            print(f"[error] unknown plan {plan}", flush=True)
            return 2

        if test_id is not None:
            if test_id not in tests:
                print(f"[error] unknown test {test_id!r} in plan {plan}", flush=True)
                return 2
            selected = {test_id: tests[test_id]}
        else:
            selected = tests

        results: list[TestResult] = []
        for tid, fn in selected.items():
            print(f"  running {tid} ...", end=" ", flush=True)
            try:
                if callable(fn) and tid in _HTTP_ONLY:
                    # Skip-stub functions take no args.
                    import inspect
                    sig = inspect.signature(fn)
                    if len(sig.parameters) == 0:
                        r = fn()
                    else:
                        r = await fn(http, base_url)
                else:
                    import inspect
                    sig = inspect.signature(fn)
                    if len(sig.parameters) == 0:
                        r = fn()
                    elif len(sig.parameters) == 2:
                        r = await fn(http, base_url)
                    else:
                        r = await fn(http, base_url, ws_base)
            except Exception as exc:
                r = TestResult(tid, False, message=f"exception: {exc}")
            results.append(r)
            status = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
            print(f"{status}  {r.message}", flush=True)

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed and not r.skipped)
    skipped = sum(1 for r in results if r.skipped)

    print(f"\n--- plan {plan} summary: {passed} passed, {failed} failed, {skipped} skipped ---")

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = [
            {"test": r.name, "passed": r.passed, "skipped": r.skipped, "message": r.message}
            for r in results
        ]
        (out_dir / "results.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n"
        )

    return 0 if failed == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Post-deploy API test runner")
    p.add_argument("--plan", type=int, required=True, choices=[1, 2, 3])
    p.add_argument("--test", default=None, help="Run a single test e.g. 1.2")
    p.add_argument("--base-url", default="http://localhost:8800")
    p.add_argument("--out-dir", type=Path, default=None, help="Write results.jsonl here")
    args = p.parse_args()

    code = asyncio.run(_run(args.plan, args.test, args.base_url, args.out_dir))
    sys.exit(code)


if __name__ == "__main__":
    main()
