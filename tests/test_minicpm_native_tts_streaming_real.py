"""GPU test: time-to-first-AUDIBLE-chunk <500ms.

@pytest.mark.gpu: requires CUDA + MiniCPM-o weights on b200.

§5.6: tightened from v1 (was: time-to-first-chunk without CN7-FIX guarantee).
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest


@pytest.mark.gpu
def test_time_to_first_audible_chunk_under_500ms():
    """Time from synthesize() call to receiving first audible PCM chunk <500ms.

    Audible means first 200ms RMS > 0.01 (not padding silence).
    Fails if CN7-FIX is not applied — pad would make first chunk ~1s of silence.
    """
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter

    _SAMPLE_RATE = 24000

    model = MiniCPMStreamingModel()
    adapter = MiniCPMNativeTtsAdapter(model)

    async def _run():
        t0 = time.monotonic()
        first_chunk = None
        async for chunk in adapter.synthesize("Hello.", []):
            first_chunk = chunk
            break
        ttfc = time.monotonic() - t0
        return first_chunk, ttfc

    first_chunk, ttfc = asyncio.run(_run())
    assert first_chunk is not None, "synthesize() yielded no chunks"

    pcm = np.frombuffer(first_chunk, dtype="<i2").astype(np.float32) / 32768.0
    n_200ms = int(_SAMPLE_RATE * 0.2)
    first_200ms = pcm[:n_200ms] if len(pcm) >= n_200ms else pcm
    rms = float(np.sqrt(np.mean(first_200ms ** 2)))

    assert rms > 0.01, (
        f"First chunk RMS={rms:.4f} — appears to be silence (CN7-FIX may be inactive). "
        f"Time-to-first-chunk={ttfc*1000:.0f}ms."
    )
    assert ttfc < 0.5, (
        f"Time to first audible chunk={ttfc*1000:.0f}ms > 500ms threshold. "
        f"Expected ~250-400ms with CN7-FIX applied."
    )
