"""GPU integration tests for CosyVoice2TtsAdapter.

Run on b200 only:
    pytest -m gpu tests/test_tts_cosyvoice2_real.py -v

Requires:
  - cosyvoice installed from FunAudioLLM git clone (NOT PyPI 0.0.8)
  - model weights at /raid/yid042/models/cosyvoice2/CosyVoice2-0.5B/
  - COSYVOICE_MODEL_DIR env var to override default path
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

_MODEL_DIR = os.environ.get(
    "COSYVOICE_MODEL_DIR",
    "/raid/yid042/models/cosyvoice2/CosyVoice2-0.5B",
)


@pytest.fixture(scope="module")
def adapter():
    from companion_harness.tts_cosyvoice2 import CosyVoice2TtsAdapter  # noqa: WPS433
    return CosyVoice2TtsAdapter(model_dir=_MODEL_DIR, warmup=True)


@pytest.mark.gpu
def test_cosyvoice2_synthesizes_chinese(adapter) -> None:
    """turn-1 (cold): '你好世界' → no exception, output > 0 bytes.

    NO TTFT assertion — first-call cost includes kernel JIT warmup.
    """
    async def _run():
        chunks = []
        async for chunk in adapter.synthesize("你好世界", []):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert len(b"".join(chunks)) > 0


@pytest.mark.gpu
def test_cosyvoice2_english_smoke(adapter) -> None:
    """Smoke test: verifies English input produces non-zero audio output.

    NO latency assertion (English quality is the qualitative gate in §1, not latency).
    """
    async def _run():
        chunks = []
        async for chunk in adapter.synthesize("Hello there", []):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_run())
    assert len(b"".join(chunks)) > 0


@pytest.mark.gpu
def test_cosyvoice2_vram_budget(adapter) -> None:
    """warmup + 3 syntheses; torch.cuda.max_memory_allocated() delta < 4 GB."""
    import torch  # noqa: WPS433
    torch.cuda.reset_peak_memory_stats()
    baseline = torch.cuda.max_memory_allocated()

    async def _run():
        for text in ["你好", "今天天气不错", "再见"]:
            async for _ in adapter.synthesize(text, []):
                pass

    asyncio.run(_run())
    peak = torch.cuda.max_memory_allocated()
    delta_gb = (peak - baseline) / (1024 ** 3)
    assert delta_gb < 4.0, f"VRAM delta {delta_gb:.2f} GB exceeds 4 GB budget"


@pytest.mark.gpu
def test_cosyvoice2_ttft_under_target(adapter) -> None:
    """turn-2+ (warm): steady-state TTFT <= 500 ms per §1 footnote.

    Warmup must have run before this test (adapter fixture does warmup=True).
    """
    async def _run():
        t0 = time.monotonic()
        first = None
        async for chunk in adapter.synthesize("你好，今天天气怎么样？", []):
            first = chunk
            break
        return (time.monotonic() - t0) * 1000, first

    ttft_ms, first_chunk = asyncio.run(_run())
    assert first_chunk is not None
    assert ttft_ms <= 500, f"TTFT {ttft_ms:.0f} ms exceeds 500 ms target"
