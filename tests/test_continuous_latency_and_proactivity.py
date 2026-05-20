"""GPU-marked latency and proactivity test for the continuous path at chunk_ms=200.

Requires real MiniCPM-o weights and CUDA; skipped automatically in CPU CI.

Success criterion (PR5a): at chunk_ms=200 (with torch.compile for headroom),
  - per-chunk wall latency p95 is within the Stage-1 barge-in budget
  - false_proactive_utterances_per_hour is within the Stage-6 target

These thresholds are evaluated against the fixture set on b200. The test is
the programmatic merge gate for PR5a (see plan §PR5a).
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

pytestmark = pytest.mark.gpu

# Stage-1 barge-in latency budget (ms). p95 per-chunk wall time must be ≤ this.
_BARGE_IN_LATENCY_BUDGET_P95_MS = 400
# Stage-6 false proactive utterance rate target (per hour of audio).
_FALSE_PROACTIVE_PER_HOUR_TARGET = 5.0


def test_continuous_latency_and_proactivity() -> None:
    """Measure per-chunk latency p95 and false-proactive rate at chunk_ms=200.

    Skipped on CPU CI (no GPU / no real weights).
    """
    try:
        import torch
        if not torch.cuda.is_available():
            pytest.skip("CUDA not available")
    except ImportError:
        pytest.skip("torch not installed")

    try:
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    except ImportError:
        pytest.skip("foreground_model_minicpm not importable (missing transformers)")

    chunk_ms = 200
    model = MiniCPMStreamingModel(chunk_ms=chunk_ms, enable_torch_compile=True)
    chunk_samples = model._chunk_samples  # 3200

    # Use silence as the fixture (no real audio needed for latency measurement).
    n_chunks = 30  # 6 seconds of audio at 200 ms
    silence = np.zeros(chunk_samples, dtype=np.int16).tobytes()

    async def _run() -> tuple[list[float], int]:
        audio_in: asyncio.Queue = asyncio.Queue()
        for i in range(n_chunks):
            await audio_in.put((silence, f"evt-{i}"))
        await audio_in.put((b"", ""))

        latencies: list[float] = []
        false_proactive = 0
        t0 = time.monotonic()
        async for is_listen, text, kv_len, evt_id in model.stream_chunks(audio_in):
            t1 = time.monotonic()
            latencies.append((t1 - t0) * 1000)
            t0 = t1
            if not is_listen and text:
                false_proactive += 1
        return latencies, false_proactive

    latencies, false_proactive = asyncio.run(_run())

    assert latencies, "no chunks yielded"
    p95 = float(np.percentile(latencies, 95))
    audio_hours = (n_chunks * chunk_ms / 1000) / 3600
    false_proactive_per_hour = false_proactive / audio_hours if audio_hours > 0 else 0.0

    assert p95 <= _BARGE_IN_LATENCY_BUDGET_P95_MS, (
        f"p95 per-chunk latency {p95:.1f} ms exceeds budget {_BARGE_IN_LATENCY_BUDGET_P95_MS} ms"
    )
    assert false_proactive_per_hour <= _FALSE_PROACTIVE_PER_HOUR_TARGET, (
        f"false proactive rate {false_proactive_per_hour:.1f}/hr exceeds target {_FALSE_PROACTIVE_PER_HOUR_TARGET}/hr"
    )
