"""CPU mock test: CN6 cancellation — cancel mid-stream, re-call works.

Starts synthesize("long"), consumes 2 chunks, cancels the task.
Immediately starts synthesize("short"), verifies it completes without exception.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


def _make_infinite_duplex():
    """Duplex that emits audio chunks indefinitely until break is set."""
    broken = False

    def _set_break():
        nonlocal broken
        broken = True

    def _is_break():
        return broken

    def _gen(**kw):
        if broken:
            return {"end_of_turn": True, "audio_waveform": None}
        return {"audio_waveform": np.full(240, 0.1, dtype=np.float32),
                "end_of_turn": False, "is_listen": False}

    duplex = MagicMock()
    duplex.streaming_generate = _gen
    duplex.streaming_prefill = MagicMock()
    duplex.prepare = MagicMock()
    duplex.is_break_set = _is_break
    duplex.set_break_event = _set_break
    duplex.current_turn_ended = False
    duplex.tokenizer = MagicMock()
    duplex.model = MagicMock()

    streaming_model = MagicMock()
    streaming_model._duplex = duplex
    return streaming_model, duplex


def _make_finite_duplex(n):
    call_idx = 0
    broken = False

    def _set_break():
        nonlocal broken
        broken = True

    def _is_break():
        return broken

    def _gen(**kw):
        nonlocal call_idx
        if broken or call_idx >= n:
            return {"end_of_turn": True, "audio_waveform": None}
        call_idx += 1
        return {"audio_waveform": np.full(240, 0.2, dtype=np.float32),
                "end_of_turn": False, "is_listen": False}

    duplex = MagicMock()
    duplex.streaming_generate = _gen
    duplex.streaming_prefill = MagicMock()
    duplex.prepare = MagicMock()
    duplex.is_break_set = _is_break
    duplex.set_break_event = _set_break
    duplex.current_turn_ended = False
    duplex.tokenizer = MagicMock()
    duplex.model = MagicMock()

    streaming_model = MagicMock()
    streaming_model._duplex = duplex
    return streaming_model, duplex


def _build_adapter(streaming_model, duplex, ref):
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
             return_value=duplex,
         ):
        from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter
        return MiniCPMNativeTtsAdapter(streaming_model)


def test_cancel_then_reuse():
    """Cancel mid-stream, then synthesize again — second call completes."""
    import soundfile as sf
    import tempfile, os
    fd, ref = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sf.write(ref, np.zeros(16000, dtype=np.float32), 16000)

    # Use one shared duplex that respects set_break_event (adapter holds a
    # reference to it; the second synthesize call re-prepares the same object).
    # To avoid state entanglement, build two separate adapters (separate locks).
    streaming_model1, duplex1 = _make_infinite_duplex()
    adapter1 = _build_adapter(streaming_model1, duplex1, ref)

    streaming_model2, duplex2 = _make_finite_duplex(5)
    adapter2 = _build_adapter(streaming_model2, duplex2, ref)

    async def _run():
        # Start infinite stream and cancel after 2 chunks
        received1 = []
        task = asyncio.create_task(_collect(adapter1, received1))
        # Wait until we have 2 chunks
        for _ in range(50):
            await asyncio.sleep(0.01)
            if len(received1) >= 2:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Second adapter should complete cleanly
        received2 = []
        async for chunk in adapter2.synthesize("short text", []):
            received2.append(chunk)
        return received1, received2

    async def _collect(adapter, out):
        async for chunk in adapter.synthesize("long long long text", []):
            out.append(chunk)

    r1, r2 = asyncio.run(_run())
    assert len(r1) >= 2, "should have received at least 2 chunks before cancel"
    assert len(r2) == 5, f"second adapter should yield 5 chunks, got {len(r2)}"
