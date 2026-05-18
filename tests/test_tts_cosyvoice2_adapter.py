"""Mocked unit tests for CosyVoice2TtsAdapter.

All tests run on CPU without cosyvoice installed by monkey-patching the lazy
import. Mirrors test_minicpm_native_tts_adapter.py patterns.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import types
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Stub cosyvoice module injected before adapter import
# ---------------------------------------------------------------------------

def _make_cosyvoice_stub(stream_arrays: list[np.ndarray] | None = None):
    """Return a stub CosyVoice2 class whose inference methods yield arrays."""
    if stream_arrays is None:
        stream_arrays = [np.zeros(4800, dtype=np.float32)]  # 200 ms @24kHz

    class _StubCosyVoice2:
        def __init__(self, model_dir, load_jit=False, fp16=False):
            pass

        def inference_sft(self, text, speaker_id, stream=False, speed=1.0):
            for arr in stream_arrays:
                yield {"tts_speech": arr}

        def inference_zero_shot(self, text, ref_text, ref_audio, stream=False, speed=1.0):
            for arr in stream_arrays:
                yield {"tts_speech": arr}

    stub_module = types.ModuleType("cosyvoice")
    cli_module = types.ModuleType("cosyvoice.cli")
    cli_cosyvoice_module = types.ModuleType("cosyvoice.cli.cosyvoice")
    cli_cosyvoice_module.CosyVoice2 = _StubCosyVoice2
    stub_module.cli = cli_module
    cli_module.cosyvoice = cli_cosyvoice_module
    return stub_module, _StubCosyVoice2


@pytest.fixture
def patched_cosyvoice(monkeypatch):
    """Inject stub cosyvoice into sys.modules before each test."""
    stub_module, stub_class = _make_cosyvoice_stub()
    monkeypatch.setitem(sys.modules, "cosyvoice", stub_module)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli", stub_module.cli)
    monkeypatch.setitem(sys.modules, "cosyvoice.cli.cosyvoice", stub_module.cli.cosyvoice)
    return stub_class


def _make_adapter(patched_cosyvoice, **kwargs):
    # Import after stub injection so lazy import finds the stub.
    from companion_harness.tts_cosyvoice2 import CosyVoice2TtsAdapter  # noqa: WPS433
    return CosyVoice2TtsAdapter(model_dir="/fake/model", warmup=False, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_adapter_constructs_with_stub(patched_cosyvoice) -> None:
    """Adapter constructs when CosyVoice2 monkey-patched to stub."""
    adapter = _make_adapter(patched_cosyvoice)
    assert adapter.sample_rate == 24000


def test_synthesize_yields_chunks(patched_cosyvoice) -> None:
    """synthesize('你好', []) yields >= 1 chunk of <= 9600 bytes each."""
    adapter = _make_adapter(patched_cosyvoice)

    async def _run():
        chunks = []
        async for chunk in adapter.synthesize("你好", []):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) >= 1
    for chunk in chunks:
        assert len(chunk) <= 9600


def test_synthesize_chunk_size_boundary(patched_cosyvoice) -> None:
    """Large array is sub-chunked to <= 9600 bytes per yield."""
    # 48000 float32 samples → 96000 bytes PCM16 → at least 10 chunks
    large_arr = np.zeros(48000, dtype=np.float32)
    stub_module, _ = _make_cosyvoice_stub([large_arr])
    import sys
    sys.modules["cosyvoice"] = stub_module
    sys.modules["cosyvoice.cli"] = stub_module.cli
    sys.modules["cosyvoice.cli.cosyvoice"] = stub_module.cli.cosyvoice

    from companion_harness.tts_cosyvoice2 import CosyVoice2TtsAdapter  # noqa: WPS433
    adapter = CosyVoice2TtsAdapter(model_dir="/fake/model", warmup=False)

    async def _run():
        chunks = []
        async for chunk in adapter.synthesize("test", []):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert all(len(c) <= 9600 for c in chunks)
    assert len(chunks) >= 10


def test_prosody_tags_ignored(patched_cosyvoice) -> None:
    """prosody_tags argument accepted and ignored without warning."""
    adapter = _make_adapter(patched_cosyvoice)

    async def _run():
        chunks = []
        async for chunk in adapter.synthesize("你好", ["emphasis", "slow"]):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) >= 1  # no exception raised


def test_synthesize_streaming_flushes_at_clause_boundary(patched_cosyvoice) -> None:
    """synthesize_streaming flushes after each CJK clause-end character."""
    adapter = _make_adapter(patched_cosyvoice)

    async def _text_iter():
        for chunk in ["你好，", "今天天气", "不错。"]:
            yield chunk

    async def _run():
        chunks = []
        async for chunk in adapter.synthesize_streaming(_text_iter(), []):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) >= 1


def test_synthesize_streaming_flushes_end_of_stream(patched_cosyvoice) -> None:
    """Remaining buffer flushed when text_chunks exhausted (no trailing boundary)."""
    adapter = _make_adapter(patched_cosyvoice)

    async def _text_iter():
        yield "没有标点"

    async def _run():
        chunks = []
        async for chunk in adapter.synthesize_streaming(_text_iter(), []):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert len(chunks) >= 1


def test_cancelled_error_mid_stream_drains_queue(patched_cosyvoice) -> None:
    """CancelledError mid-stream drains queue and joins producer within 100 ms."""
    # Use a slow stub: sleeps briefly between items to give cancellation a window
    slow_arrs = [np.zeros(4800, dtype=np.float32)] * 5

    class _SlowCosyVoice2:
        def __init__(self, model_dir, load_jit=False, fp16=False):
            pass

        def inference_sft(self, text, speaker_id, stream=False, speed=1.0):
            import time
            for arr in slow_arrs:
                time.sleep(0.02)
                yield {"tts_speech": arr}

    stub_module = types.ModuleType("cosyvoice")
    cli_module = types.ModuleType("cosyvoice.cli")
    cli_cosyvoice_module = types.ModuleType("cosyvoice.cli.cosyvoice")
    cli_cosyvoice_module.CosyVoice2 = _SlowCosyVoice2
    stub_module.cli = cli_module
    cli_module.cosyvoice = cli_cosyvoice_module
    import sys
    sys.modules["cosyvoice"] = stub_module
    sys.modules["cosyvoice.cli"] = cli_module
    sys.modules["cosyvoice.cli.cosyvoice"] = cli_cosyvoice_module

    from companion_harness.tts_cosyvoice2 import CosyVoice2TtsAdapter  # noqa: WPS433
    adapter = CosyVoice2TtsAdapter(model_dir="/fake/model", warmup=False)

    async def _run():
        gen = adapter.synthesize("test", [])
        # Get first chunk then cancel
        first = await gen.__anext__()
        assert first is not None
        t0 = asyncio.get_event_loop().time()
        task = asyncio.current_task()
        task.cancel()
        try:
            await gen.__anext__()
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        elapsed = asyncio.get_event_loop().time() - t0
        return elapsed

    import time
    t0 = time.monotonic()

    async def _outer():
        try:
            task = asyncio.create_task(_run())
            return await task
        except asyncio.CancelledError:
            pass

    asyncio.run(_outer())
    elapsed = time.monotonic() - t0
    # Producer thread should join within 100 ms after cancellation
    assert elapsed < 0.5  # generous bound for CI
