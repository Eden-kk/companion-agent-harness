"""KokoroTtsAdapter sub-chunking — CPU-runnable contract tests.

These tests stub ``self._kokoro`` with a fake that returns known PCM so no
model weights are required.  They verify that:

1. Every yielded chunk is ≤ 9600 bytes (200 ms at 24 kHz mono PCM16).
2. Concatenating all yielded chunks produces the same bytes as the un-sliced
   raw PCM (i.e., sub-chunking is byte-identical to the original).
3. Short utterances whose PCM fits in one chunk are still yielded as exactly
   one chunk (no unnecessary slicing overhead).
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

pytest.importorskip("kokoro_onnx")
from companion_harness.tts_kokoro import KokoroTtsAdapter, _MAX_CHUNK_BYTES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_float32_samples(n_samples: int) -> np.ndarray:
    """Return a deterministic float32 waveform of length *n_samples*."""
    rng = np.random.default_rng(42)
    return rng.uniform(-1.0, 1.0, n_samples).astype(np.float32)


def _float32_to_expected_bytes(samples: np.ndarray) -> bytes:
    """Apply the same float32→int16 conversion used by KokoroTtsAdapter."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


async def _fake_create_stream(chunks: list[np.ndarray]):
    """Async generator that yields (samples, sample_rate) tuples."""
    for chunk in chunks:
        yield chunk, 24000


def _make_adapter_with_fake(chunks: list[np.ndarray]) -> KokoroTtsAdapter:
    """Build a KokoroTtsAdapter whose internal _kokoro yields *chunks*."""
    fake_kokoro = MagicMock()
    fake_kokoro.voices = {"af_bella"}

    async def create_stream_side_effect(*args, **kwargs) -> AsyncIterator:
        async for item in _fake_create_stream(chunks):
            yield item

    fake_kokoro.create_stream = create_stream_side_effect

    with patch("companion_harness.tts_kokoro.Kokoro", return_value=fake_kokoro):
        adapter = KokoroTtsAdapter(
            model_path="/fake/model.onnx",
            voices_path="/fake/voices.json",
            warmup=False,
        )
    return adapter


async def _collect_chunks(adapter: KokoroTtsAdapter, text: str) -> list[bytes]:
    chunks: list[bytes] = []
    async for chunk in adapter.synthesize(text, []):
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kokoro_yields_subchunks_under_200ms() -> None:
    """Every yielded chunk must be ≤ _MAX_CHUNK_BYTES (200 ms at 24 kHz mono PCM16)."""
    # ~3 s at 24 kHz = 72 000 samples → 144 000 bytes raw PCM; well above 9600
    n_samples = 72_000
    samples = _make_float32_samples(n_samples)
    adapter = _make_adapter_with_fake([samples])

    chunks = await _collect_chunks(adapter, "a long phrase")

    assert len(chunks) > 1, "Expected multiple sub-chunks for a 3-s utterance"
    for i, chunk in enumerate(chunks):
        assert len(chunk) <= _MAX_CHUNK_BYTES, (
            f"Chunk {i} has {len(chunk)} bytes, exceeds {_MAX_CHUNK_BYTES}"
        )


@pytest.mark.asyncio
async def test_kokoro_concat_byte_identical_to_unsliced() -> None:
    """Concatenation of all sub-chunks must equal the un-sliced PCM bytes."""
    n_samples = 72_000
    samples = _make_float32_samples(n_samples)
    expected = _float32_to_expected_bytes(samples)

    adapter = _make_adapter_with_fake([samples])
    chunks = await _collect_chunks(adapter, "a long phrase")

    assert b"".join(chunks) == expected


@pytest.mark.asyncio
async def test_kokoro_short_utterance_still_yields_one_chunk() -> None:
    """A short utterance whose PCM fits within 200 ms should yield exactly one chunk."""
    # 100 ms at 24 kHz = 2400 samples → 4800 bytes (< 9600)
    n_samples = 2_400
    samples = _make_float32_samples(n_samples)
    adapter = _make_adapter_with_fake([samples])

    chunks = await _collect_chunks(adapter, "hi")

    assert len(chunks) == 1
    assert len(chunks[0]) == n_samples * 2  # 2 bytes per int16 sample


@pytest.mark.asyncio
async def test_kokoro_multiple_kokoro_chunks_each_subchunked() -> None:
    """Sub-chunking applies independently to each chunk emitted by create_stream."""
    # Two chunks from Kokoro: first ~3 s, second ~1.5 s
    s1 = _make_float32_samples(72_000)
    s2 = _make_float32_samples(36_000)
    expected = _float32_to_expected_bytes(s1) + _float32_to_expected_bytes(s2)

    adapter = _make_adapter_with_fake([s1, s2])
    chunks = await _collect_chunks(adapter, "a longer phrase with two segments")

    for i, chunk in enumerate(chunks):
        assert len(chunk) <= _MAX_CHUNK_BYTES, (
            f"Chunk {i} has {len(chunk)} bytes, exceeds {_MAX_CHUNK_BYTES}"
        )
    assert b"".join(chunks) == expected
