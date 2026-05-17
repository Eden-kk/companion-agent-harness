"""Probe: torch.compile MiniCPM-o warmup gate (Plan 3 PR1 §2.1).

Run on B200 before shipping --torch-compile to production:

    /raid/yid042/venvs/companion-harness/bin/python scripts/probe_torch_compile_warmup.py

Pass criterion: warm latency on cycle 8 within 30% of cycle 1 (no progressive
degradation), and ≥1.2× speedup vs cold latency (per operator-confirmed §8).

Prints per-cycle latencies, ratio, and a SHIP / NO-SHIP verdict.
"""

from __future__ import annotations

import asyncio
import math
import sys
import time

import numpy as np


_SAMPLE_RATE = 16000
_SILENT_FRAMES_5S = bytes(_SAMPLE_RATE * 2 * 5)  # 5 s, 16 kHz, int16, silence
_ADDRESSING_PROMPT = "Is the agent being addressed? Transcript: hello."
_N_RESET_CYCLES = 8
_MAX_DEGRADATION_RATIO = 1.30  # cycle-8 / cycle-1 must be < this
_MIN_SPEEDUP_RATIO = 1.20      # cold / warm must be >= this to ship


async def _drain_infer(model: object, pcm: bytes) -> float:
    """Return wall-time (seconds) from start of infer_stream to first proposal
    (or full drain if no proposal).  Returns elapsed seconds."""

    async def _frames():
        yield pcm, None

    t0 = time.monotonic()
    proposals = 0
    listen_count = 0
    gen = await model.infer_stream(_frames(), caused_by=["probe"])  # type: ignore[attr-defined]
    async for _ in gen:
        proposals += 1
        if proposals >= 1:
            break
    elapsed = time.monotonic() - t0

    if proposals == 0:
        # Check is_listen invocations via attribute
        listen_count = getattr(model, "_last_is_listen", None)
        if listen_count is None:
            print("  warn: no proposal and no _last_is_listen attribute", flush=True)
    return elapsed


def _warm_cycle(model: object, pcm: bytes) -> float:
    """One reset+infer cycle. Returns wall-time from reset entry to first proposal."""

    async def _run() -> float:
        t0 = time.monotonic()
        model.reset_streaming_session(caused_by=["probe"])  # type: ignore[attr-defined]
        elapsed = await _drain_infer(model, pcm)
        return time.monotonic() - t0

    return asyncio.get_event_loop().run_until_complete(_run())


def main() -> int:
    print("probe_torch_compile_warmup: loading MiniCPMStreamingModel(enable_torch_compile=True)...")
    print("  (expect ~20-30 s compile cost on first inference call)", flush=True)

    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: WPS433
    model = MiniCPMStreamingModel(enable_torch_compile=True)

    if not model._torch_compile_active:
        print("FAIL: torch.compile did not activate (_torch_compile_active=False). "
              "Check CUDA availability and torch version.", flush=True)
        return 1

    print(f"  _torch_compile_active=True", flush=True)

    # Step 1: classify_yes_no (addressing-classifier shape compile)
    print(f"\nStep 1: classify_yes_no — compile addressing-classifier shape...", flush=True)
    t0 = time.monotonic()
    try:
        is_yes, prob = model.classify_yes_no(_ADDRESSING_PROMPT)
    except Exception as exc:
        print(f"FAIL: classify_yes_no raised {type(exc).__name__}: {exc}", flush=True)
        return 1
    classify_ms = round((time.monotonic() - t0) * 1000)
    if not math.isfinite(prob):
        print(f"FAIL: classify_yes_no returned non-finite probability: {prob}", flush=True)
        return 1
    print(f"  classify_yes_no: is_yes={is_yes} prob={prob:.3f} elapsed={classify_ms}ms", flush=True)

    # Step 2: cold infer_stream (first call — compile cost paid here)
    print(f"\nStep 2: cold infer_stream (5 s silent PCM)...", flush=True)
    t_cold_start = time.monotonic()
    try:
        cold_elapsed = asyncio.get_event_loop().run_until_complete(
            _drain_infer(model, _SILENT_FRAMES_5S)
        )
    except Exception as exc:
        print(f"FAIL: cold infer_stream raised {type(exc).__name__}: {exc}", flush=True)
        return 1
    cold_ms = round(cold_elapsed * 1000)
    print(f"  cold latency: {cold_ms}ms", flush=True)

    # Step 3: 8 reset+infer cycles
    print(f"\nStep 3: {_N_RESET_CYCLES} reset+infer cycles...", flush=True)
    warm_latencies_ms: list[int] = []
    for cycle in range(1, _N_RESET_CYCLES + 1):
        try:
            elapsed = _warm_cycle(model, _SILENT_FRAMES_5S)
        except Exception as exc:
            print(f"FAIL: cycle {cycle} raised {type(exc).__name__}: {exc}", flush=True)
            return 1
        ms = round(elapsed * 1000)
        warm_latencies_ms.append(ms)
        print(f"  cycle {cycle:2d}: {ms}ms", flush=True)

    # Results
    cycle1_ms = warm_latencies_ms[0]
    cycle8_ms = warm_latencies_ms[-1]
    degradation_ratio = cycle8_ms / cycle1_ms if cycle1_ms > 0 else float("inf")
    speedup_ratio = cold_ms / cycle1_ms if cycle1_ms > 0 else 0.0

    print()
    print("=" * 60)
    print(f"Cold latency (step 2):          {cold_ms}ms")
    print(f"Cycle-1 warm latency:           {cycle1_ms}ms")
    print(f"Cycle-8 warm latency:           {cycle8_ms}ms")
    print(f"Cold/warm speedup ratio:        {speedup_ratio:.2f}x  (need >= {_MIN_SPEEDUP_RATIO:.2f}x)")
    print(f"Cycle-8/cycle-1 degrade ratio:  {degradation_ratio:.2f}x  (need < {_MAX_DEGRADATION_RATIO:.2f}x)")
    print()

    speedup_ok = speedup_ratio >= _MIN_SPEEDUP_RATIO
    degrade_ok = degradation_ratio < _MAX_DEGRADATION_RATIO

    if speedup_ok and degrade_ok:
        print("VERDICT: PASS — SHIP")
        print("  torch.compile delivers >=1.2x speedup with no progressive KV drift.")
        return 0
    else:
        reasons = []
        if not speedup_ok:
            reasons.append(
                f"speedup {speedup_ratio:.2f}x < {_MIN_SPEEDUP_RATIO:.2f}x threshold"
            )
        if not degrade_ok:
            reasons.append(
                f"cycle-8/cycle-1 ratio {degradation_ratio:.2f}x >= {_MAX_DEGRADATION_RATIO:.2f}x "
                f"(DynamicCache drift suspected)"
            )
        print("VERDICT: FAIL — NO-SHIP")
        for r in reasons:
            print(f"  - {r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
