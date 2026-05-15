"""Silero VAD wrapper implementing the `VADModel` Protocol.

Uses the bundled ONNX checkpoint that ships with the `silero-vad` PyPI package
(version 6.x). The ONNX runtime path is preferred over the JIT/torch.hub path
because the canonical b200 venv has torch 2.11 but no compatible torchaudio,
which the silero-vad python entry point requires.

Per-frame call pattern:
  - Caller provides PCM16 little-endian mono frames sampled at 16 kHz.
  - Silero's window is 512 samples (32 ms). If the caller passes a longer
    frame, only the first 512 samples are scored (the frame_duration_ms
    contract of the harness is 32 ms, so this should be exact).

State:
  - Silero is stateful (recurrent). State carries across calls within a
    session. Call `reset_states()` to clear between sessions.

Determinism (invariant #5):
  - The ONNX session is deterministic given the same input + state.
  - We do not seed PyTorch because we never enter PyTorch.
"""

from __future__ import annotations

import array
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["SileroVADModel"]

# 512 samples = 32 ms @ 16 kHz; matches frame_duration_ms used by VADDetector.
_WINDOW_SAMPLES = 512
_SAMPLE_RATE = 16000
# State shape per silero v6 ONNX: (2, batch=1, 128)
_STATE_SHAPE = (2, 1, 128)


class SileroVADModel:
    """VADModel adapter for Silero VAD (ONNX runtime path).

    Lazy-imports onnxruntime so this module can be imported on machines
    without it (e.g. local dev). The model file ships with the silero-vad
    PyPI package; we locate it via `silero_vad.__file__`.
    """

    def __init__(self, model_path: str | Path | None = None) -> None:
        # Lazy imports — keep module-level import cheap.
        import onnxruntime as ort  # type: ignore

        if model_path is None:
            # Locate the silero-vad package directory by filesystem lookup.
            # We deliberately do NOT `import silero_vad` because that package's
            # __init__ imports torchaudio, which fails on the b200 venv
            # (torch 2.11 + libcudart.so.12 vs torchaudio's libcudart.so.13).
            # The ONNX checkpoint lives next to the python package files.
            import importlib.util as _ilu

            spec = _ilu.find_spec("silero_vad")
            if spec is None or spec.origin is None:
                raise RuntimeError(
                    "silero-vad package not installed; install via "
                    "`pip install silero-vad`"
                )
            pkg_dir = Path(spec.origin).parent
            model_path = pkg_dir / "data" / "silero_vad.onnx"

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 1
        sess_opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(model_path), sess_options=sess_opts, providers=["CPUExecutionProvider"]
        )
        self._state: np.ndarray = np.zeros(_STATE_SHAPE, dtype=np.float32)
        self._sr = np.array(_SAMPLE_RATE, dtype=np.int64)

    def reset_states(self) -> None:
        """Reset Silero's recurrent state. Call between sessions."""
        self._state = np.zeros(_STATE_SHAPE, dtype=np.float32)

    def __call__(self, frame: bytes) -> float:
        """Return p_speech in [0, 1] for the leading 32 ms of the frame."""
        if len(frame) < 2:
            return 0.0
        # PCM16 LE → float32 in [-1, 1].
        # Trim to even byte count (defensive).
        usable = frame[: len(frame) - len(frame) % 2]
        samples = array.array("h", usable)
        n = min(len(samples), _WINDOW_SAMPLES)
        if n == 0:
            return 0.0
        audio = np.zeros(_WINDOW_SAMPLES, dtype=np.float32)
        # Copy first WINDOW_SAMPLES samples, normalize int16 → float.
        audio[:n] = np.frombuffer(usable, dtype=np.int16)[:n].astype(np.float32) / 32768.0
        audio = audio.reshape(1, _WINDOW_SAMPLES)

        out, state_n = self._session.run(
            None, {"input": audio, "state": self._state, "sr": self._sr}
        )
        self._state = state_n
        return float(out[0, 0])
