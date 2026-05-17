"""CPU tests for MiniCPMNativeTtsAdapter streaming path — stub duplex.

Tests chunk emission shape and cancellation without loading any model.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers — build a minimal fake streaming_model + duplex
# ---------------------------------------------------------------------------

def _make_fake_streaming_model(chunks):
    """Return a fake streaming_model whose TTS duplex emits `chunks`.

    Each element of chunks is either a numpy float32 array (audio) or None
    (end-of-turn). A final None sentinel is always appended.
    """
    call_idx = 0

    def _streaming_generate(**kw):
        nonlocal call_idx
        if call_idx >= len(chunks):
            return {"end_of_turn": True, "audio_waveform": None}
        item = chunks[call_idx]
        call_idx += 1
        if item is None:
            return {"end_of_turn": True, "audio_waveform": None}
        return {"end_of_turn": False, "is_listen": False, "audio_waveform": item}

    duplex = MagicMock()
    duplex.streaming_generate = _streaming_generate
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


def _silence_wav(n=480):
    return np.zeros(n, dtype=np.float32)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_streaming_mock_emits_chunks():
    """Adapter collects PCM16 bytes from stub duplex without dropping any."""
    wav1 = np.full(240, 0.5, dtype=np.float32)
    wav2 = np.full(240, -0.3, dtype=np.float32)
    streaming_model, _duplex = _make_fake_streaming_model([wav1, wav2, None])

    import soundfile as sf
    import tempfile, os
    fd, ref = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sf.write(ref, np.zeros(16000, dtype=np.float32), 16000)

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
        chunks = []
        async for c in adapter.synthesize("Hello.", []):
            chunks.append(c)
        return chunks

    result = asyncio.run(_run())
    assert len(result) == 2, f"expected 2 chunks, got {len(result)}"
    total_bytes = sum(len(c) for c in result)
    assert total_bytes == (240 + 240) * 2  # float32→int16 = 2 bytes each


def test_streaming_mock_empty_wav_skipped():
    """Chunks with zero-length audio_waveform are not yielded."""
    empty = np.zeros(0, dtype=np.float32)
    real = np.full(100, 0.1, dtype=np.float32)
    streaming_model, _duplex = _make_fake_streaming_model([empty, real, None])

    import soundfile as sf
    import tempfile, os
    fd, ref = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sf.write(ref, np.zeros(16000, dtype=np.float32), 16000)

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
        chunks = []
        async for c in adapter.synthesize("Hi.", []):
            chunks.append(c)
        return chunks

    result = asyncio.run(_run())
    assert len(result) == 1
    assert len(result[0]) == 100 * 2


def test_streaming_mock_none_audio_waveform_skipped():
    """Chunks where audio_waveform is None are not yielded."""
    streaming_model, _duplex = _make_fake_streaming_model([None])  # immediate end

    import soundfile as sf
    import tempfile, os
    fd, ref = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sf.write(ref, np.zeros(16000, dtype=np.float32), 16000)

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
        chunks = []
        async for c in adapter.synthesize(".", []):
            chunks.append(c)
        return chunks

    result = asyncio.run(_run())
    assert result == []
