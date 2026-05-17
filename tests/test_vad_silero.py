"""Regression tests for SileroVADModel context-window fix (2026-05-17).

Without the 64-sample context prepend, Silero ONNX returns p_speech ~0.002
on ALL audio — VAD never fires vad_turn_signal.
"""

import numpy as np
import pytest

from companion_harness.vad_silero import SileroVADModel


def test_silero_produces_speech_probability_above_zero_on_speech_like_audio():
    """Regression for 2026-05-17 context-window bug.

    Without the 64-sample context prepend, Silero ONNX returns p_speech ~0.002
    on ALL audio. With the fix, multi-frequency synthetic speech should produce
    p_speech > 0.2 within 10 frames.
    """
    model = SileroVADModel()
    # Speech-like: sum of 5 harmonics
    speech = np.zeros(512, dtype=np.float32)
    for freq in [200, 400, 800, 1600, 3200]:
        speech += np.sin(2 * np.pi * freq * np.arange(512) / 16000.0).astype(np.float32) * 0.1
    frame = (speech * 32768).astype(np.int16).tobytes()

    p_max = max(model(frame) for _ in range(10))
    assert p_max > 0.2, f"p_max={p_max:.4f}; context-window prepend may be broken"


def test_silero_reset_states_clears_context():
    """reset_states must clear both _state and _context."""
    model = SileroVADModel()
    # Feed some audio to populate context
    speech = (np.random.randn(512).astype(np.float32) * 0.3 * 32768).astype(np.int16).tobytes()
    model(speech)
    # State and context should be non-zero now
    assert not np.allclose(model._context, 0), "context should be populated after first call"
    model.reset_states()
    assert np.allclose(model._context, 0), "reset_states must clear _context"
