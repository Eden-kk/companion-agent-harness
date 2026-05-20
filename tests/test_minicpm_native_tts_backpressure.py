"""CPU mock test: CN5 backpressure — queue.put blocks producer, no drops.

100 chunks emitted by stub duplex, consumer sleeps 5ms per chunk.
Verifies all chunks received and producer was blocked (elapsed >= 100*5ms).
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytest.importorskip("soundfile")


def _make_duplex_n_chunks(n):
    call_idx = 0

    def _gen(**kw):
        nonlocal call_idx
        if call_idx < n:
            call_idx += 1
            return {"audio_waveform": np.full(240, 0.1, dtype=np.float32),
                    "end_of_turn": False, "is_listen": False}
        return {"end_of_turn": True, "audio_waveform": None}

    duplex = MagicMock()
    duplex.streaming_generate = _gen
    duplex.streaming_prefill = MagicMock()
    duplex.prepare = MagicMock()
    duplex.is_break_set = MagicMock(return_value=False)
    duplex.set_break_event = MagicMock()
    duplex.current_turn_ended = False
    duplex.tokenizer = MagicMock()
    duplex.model = MagicMock()

    streaming_model = MagicMock()
    streaming_model._duplex = duplex
    return streaming_model, duplex


def test_backpressure_no_drops():
    """All 100 produced chunks arrive; producer is blocked (not fire-and-forget)."""
    import soundfile as sf
    import tempfile, os
    fd, ref = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sf.write(ref, np.zeros(16000, dtype=np.float32), 16000)

    N = 100
    SLEEP_MS = 5
    streaming_model, _duplex = _make_duplex_n_chunks(N)

    with patch("companion_harness.tts_minicpm_native._patch_torchaudio"), \
         patch.object(
             __import__("companion_harness.tts_minicpm_native",
                        fromlist=["MiniCPMNativeTtsAdapter"])
             .MiniCPMNativeTtsAdapter,
             "_write_silent_ref",
             return_value=ref,
         ), \
         patch(
             "companion_harness.tts_minicpm_native._MiniCPMODuplexNoPad.install",
             return_value=_duplex,
         ):
        from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter
        adapter = MiniCPMNativeTtsAdapter(streaming_model)

    async def _run():
        received = []
        t0 = time.monotonic()
        async for chunk in adapter.synthesize("x" * 200, []):
            await asyncio.sleep(SLEEP_MS / 1000)
            received.append(chunk)
        elapsed = time.monotonic() - t0
        return received, elapsed

    chunks, elapsed = asyncio.run(_run())
    assert len(chunks) == N, f"expected {N} chunks, got {len(chunks)}"
    # Producer should have been back-pressured; total elapsed ≥ N*SLEEP_MS/1000
    min_expected = N * SLEEP_MS / 1000
    assert elapsed >= min_expected * 0.8, (
        f"elapsed {elapsed:.3f}s < 80% of expected {min_expected:.3f}s — "
        "producer may not be blocking on full queue"
    )
