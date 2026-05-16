"""Tests for MiniCPMNativeDuplexEouSource (v0.1j Task 8).

GPU-conditional: skips cleanly without CUDA (b200 venv required).
Non-GPU tests run everywhere.
"""

from __future__ import annotations

import math
import struct

import pytest

from companion_harness.native_duplex_eou import (
    MiniCPMNativeDuplexEouSource,
    NativeDuplexEouSource,
    _NullNativeDuplexEouSource,
)
from companion_harness.schemas import TurnSignal


# ---------------------------------------------------------------------------
# Non-GPU: structural / marker tests
# ---------------------------------------------------------------------------


def test_minicpm_native_duplex_eou_source_runtime_checkable_null():
    """_NullNativeDuplexEouSource satisfies NativeDuplexEouSource Protocol."""
    assert isinstance(_NullNativeDuplexEouSource(), NativeDuplexEouSource)


def test_no_unavailable_157_marker_anymore():
    """The # UNAVAILABLE: #157 marker must not appear in _NullNativeDuplexEouSource."""
    import inspect
    from companion_harness import native_duplex_eou
    source = inspect.getsource(native_duplex_eou._NullNativeDuplexEouSource)
    assert "UNAVAILABLE: #157" not in source, (
        "_NullNativeDuplexEouSource still carries the UNAVAILABLE: #157 marker — "
        "remove it now that the real impl exists."
    )


def test_module_docstring_no_unavailable_marker():
    """Module-level # UNAVAILABLE: #157 marker must be gone."""
    from companion_harness import native_duplex_eou
    assert "UNAVAILABLE: #157" not in (native_duplex_eou.__doc__ or ""), (
        "Module docstring still carries the UNAVAILABLE: #157 marker."
    )


def test_minicpm_native_duplex_eou_source_in_all():
    """MiniCPMNativeDuplexEouSource is exported from __all__."""
    from companion_harness import native_duplex_eou
    assert "MiniCPMNativeDuplexEouSource" in native_duplex_eou.__all__


# ---------------------------------------------------------------------------
# GPU tests
# ---------------------------------------------------------------------------

torch = pytest.importorskip("torch", reason="torch not available — b200 venv required")

if not torch.cuda.is_available():
    pytest.skip("CUDA not available — b200 required", allow_module_level=True)

from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel


def _make_pcm16_silence(seconds: float, sample_rate: int = 16000) -> bytes:
    n_samples = int(seconds * sample_rate)
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


def _make_pcm16_tone(freq_hz: float, seconds: float, sample_rate: int = 16000, amplitude: float = 0.3) -> bytes:
    n_samples = int(seconds * sample_rate)
    samples = [
        int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


@pytest.fixture(scope="module")
def streaming_model():
    return MiniCPMStreamingModel()


@pytest.mark.gpu
def test_minicpm_native_duplex_eou_source_runtime_checkable(streaming_model):
    """MiniCPMNativeDuplexEouSource satisfies NativeDuplexEouSource Protocol."""
    src = MiniCPMNativeDuplexEouSource(streaming_model)
    assert isinstance(src, NativeDuplexEouSource)


@pytest.mark.gpu
@pytest.mark.asyncio
async def test_sample_returns_turn_signal_or_none(streaming_model):
    """After driving infer_stream, get_eou_signal() returns TurnSignal or None.

    The exact return depends on whether the model decided to speak on the
    fixture audio (behavioral — invariant #6). We assert type correctness only.
    """
    src = MiniCPMNativeDuplexEouSource(streaming_model)

    audio_segments = [
        _make_pcm16_silence(1.0),
        _make_pcm16_tone(440.0, 2.0),
        _make_pcm16_silence(1.0),
    ]

    async def _frame_iter():
        for seg in audio_segments:
            yield seg, None

    gen = await streaming_model.infer_stream(_frame_iter(), caused_by=["test-eou-evt-1"])
    async for _ in gen:
        pass  # exhaust the generator so _last_is_listen is updated

    result = src.get_eou_signal()
    assert result is None or isinstance(result, TurnSignal), (
        f"get_eou_signal() must return TurnSignal or None, got {type(result)}"
    )
    if isinstance(result, TurnSignal):
        assert result.detector == "native_duplex"
        assert result.p_done == 1.0
        assert 0.0 <= result.confidence <= 1.0


@pytest.mark.gpu
def test_get_eou_signal_none_before_infer_stream(streaming_model):
    """Before any infer_stream call, _last_is_listen=True so signal is None."""
    fresh_model = MiniCPMStreamingModel()
    src = MiniCPMNativeDuplexEouSource(fresh_model)
    assert src.get_eou_signal() is None, (
        "Before any streaming_generate call, get_eou_signal() must return None"
    )
