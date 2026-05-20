"""CPU plumbing test: MiniCPMStreamingModel chunk_ms parameter.

No GPU/torch weights. Asserts:
  - chunk_ms=200 → _chunk_samples == 3200
  - chunk_ms=1000 (default) → _chunk_samples == 16000
  - stream_chunks yields one record per 3200-sample chunk at chunk_ms=200
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Fake base model — mirrors test_minicpm_torch_compile_flag._FakeBase
# but _FakeDuplex records the chunk_ms it was constructed with.
# ---------------------------------------------------------------------------

class _FakeInnerModel:
    audio_past_key_values = None


class _FakeDuplex:
    max_new_speak_tokens_per_chunk = 10
    temperature = 1.0
    top_k = 50
    top_p = 0.9
    listen_prob_scale = 1.0
    text_repetition_penalty = 1.0
    text_repetition_window_size = 5
    model = _FakeInnerModel()

    class _AlwaysEndedDesc:
        def __get__(self, obj, objtype=None) -> bool:
            return True
        def __set__(self, obj, value) -> None:
            pass

    current_turn_ended = _AlwaysEndedDesc()

    def prepare(self, **kwargs) -> None:
        pass

    def streaming_prefill(self, **kwargs) -> None:
        pass

    def streaming_generate(self, **kwargs) -> dict:
        return {"is_listen": True, "text": ""}


class _FakeBase:
    llm = MagicMock()
    device = "cpu"

    def eval(self) -> "_FakeBase":
        return self

    def cuda(self) -> "_FakeBase":
        return self

    def as_duplex(self, generate_audio: bool, sliding_window_mode: str = "off", chunk_ms: int = 1000) -> _FakeDuplex:
        self._last_chunk_ms = chunk_ms
        return _FakeDuplex()


def _patches(fake_base: _FakeBase):
    return (
        patch(
            "companion_harness.foreground_model_minicpm.AutoModel.from_pretrained",
            return_value=fake_base,
        ),
        patch(
            "companion_harness.foreground_model_minicpm.AutoTokenizer.from_pretrained",
            return_value=MagicMock(),
        ),
    )


# ---------------------------------------------------------------------------
# Tests — chunk_samples computation
# ---------------------------------------------------------------------------

def test_chunk_ms_200_sets_chunk_samples() -> None:
    fake_base = _FakeBase()
    am, at = _patches(fake_base)
    with am, at:
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
        model = MiniCPMStreamingModel(chunk_ms=200)
    assert model._chunk_samples == 3200


def test_chunk_ms_default_1000_unchanged() -> None:
    fake_base = _FakeBase()
    am, at = _patches(fake_base)
    with am, at:
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
        model = MiniCPMStreamingModel()
    assert model._chunk_samples == 16000


def test_chunk_ms_threaded_to_as_duplex() -> None:
    fake_base = _FakeBase()
    am, at = _patches(fake_base)
    with am, at:
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
        MiniCPMStreamingModel(chunk_ms=200)
    assert fake_base._last_chunk_ms == 200


# ---------------------------------------------------------------------------
# Test — stream_chunks yields one record per chunk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_chunks_yields_per_200ms_chunk() -> None:
    """Feed 3 full 200 ms chunks (3200 samples each) → 3 records yielded."""
    fake_base = _FakeBase()
    am, at = _patches(fake_base)
    with am, at:
        from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
        model = MiniCPMStreamingModel(chunk_ms=200)

    # Build audio_in queue: 3 × 3200-sample (200 ms) chunks of silence
    audio_in: asyncio.Queue = asyncio.Queue()
    chunk_samples = 3200
    silence = np.zeros(chunk_samples, dtype=np.int16).tobytes()
    for i in range(3):
        await audio_in.put((silence, f"evt-{i}"))
    await audio_in.put((b"", ""))  # sentinel

    records = []
    async for record in model.stream_chunks(audio_in):
        records.append(record)

    assert len(records) == 3
    for is_listen, text, kv_len, evt_id in records:
        assert isinstance(is_listen, bool)
        assert isinstance(text, str)
