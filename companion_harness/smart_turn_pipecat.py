"""Pipecat Smart Turn v3 wrapper implementing the `SmartTurnModel` Protocol.

Smart Turn v3 is a Whisper-Tiny-encoder + shallow-linear-head end-of-turn
classifier. We use the int8-quantized CPU ONNX checkpoint (8 MB, ~12 ms
inference per turn).

Input contract:
  - The orchestrator's SmartTurnDetector hands us the *accumulated* turn audio
    buffer at silence-candidate moments, as PCM16 little-endian mono @ 16 kHz.
  - Smart Turn v3 expects log-mel features shaped (80, 800) — i.e. 800 mel
    frames at 10 ms hop, which is 8 seconds of audio. We left-pad / right-trim
    so we always score the most-recent 8 seconds of the turn.

Output contract:
  - Smart Turn returns a single logit for "turn complete". We map it to
    (p_done, p_continue) = (sigmoid(logit), 1 - sigmoid(logit)) per the
    SmartTurnModel Protocol.

Determinism (invariant #5):
  - ONNX run is deterministic for a fixed input.
  - The Whisper feature extractor is deterministic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["PipecatSmartTurnModel"]

# Smart Turn v3.x input contract.
_SAMPLE_RATE = 16000
_TARGET_FRAMES = 800           # log-mel frames the model expects
_HOP = 160                     # WhisperFeatureExtractor default hop_length
_WINDOW_SAMPLES = _TARGET_FRAMES * _HOP  # 128000 = 8 seconds

# HF repo + filename. We look it up via huggingface_hub.snapshot_download so the
# canonical HF cache at /raid/huggingface/hub is used (HF_HUB_CACHE is set).
_HF_REPO = "pipecat-ai/smart-turn-v3"
_DEFAULT_ONNX = "smart-turn-v3.2-cpu.onnx"


def _sigmoid(x: float) -> float:
    if x >= 0:
        z = float(np.exp(-x))
        return 1.0 / (1.0 + z)
    z = float(np.exp(x))
    return z / (1.0 + z)


class PipecatSmartTurnModel:
    """SmartTurnModel adapter for Pipecat Smart Turn v3 (CPU ONNX path).

    Lazy-imports onnxruntime + transformers + huggingface_hub so this module
    can be imported on machines without them. The model + feature extractor
    are loaded once at construction.
    """

    def __init__(
        self,
        *,
        onnx_filename: str = _DEFAULT_ONNX,
        whisper_model_id: str = "openai/whisper-tiny",
    ) -> None:
        import onnxruntime as ort  # type: ignore
        from huggingface_hub import snapshot_download  # type: ignore
        from transformers import WhisperFeatureExtractor  # type: ignore

        repo_dir = Path(snapshot_download(_HF_REPO))
        onnx_path = repo_dir / onnx_filename

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 1
        sess_opts.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(onnx_path), sess_options=sess_opts, providers=["CPUExecutionProvider"]
        )
        self._feature_extractor = WhisperFeatureExtractor.from_pretrained(whisper_model_id)

    def _featurize(self, audio: np.ndarray) -> np.ndarray:
        """audio: 1-D float32 mono 16 kHz. Returns (1, 80, 800) float32 log-mel."""
        # Left-trim or left-pad to exactly _WINDOW_SAMPLES.
        if audio.shape[0] > _WINDOW_SAMPLES:
            audio = audio[-_WINDOW_SAMPLES:]
        elif audio.shape[0] < _WINDOW_SAMPLES:
            pad = _WINDOW_SAMPLES - audio.shape[0]
            audio = np.pad(audio, (pad, 0), mode="constant")

        feats = self._feature_extractor(
            audio, sampling_rate=_SAMPLE_RATE, return_tensors="np", padding=False
        )
        f = feats["input_features"]  # (1, 80, N)
        # Whisper extractor pads to 3000 frames; we want 800.
        if f.shape[2] >= _TARGET_FRAMES:
            f = f[:, :, :_TARGET_FRAMES]
        else:
            pad = _TARGET_FRAMES - f.shape[2]
            f = np.pad(f, ((0, 0), (0, 0), (0, pad)), mode="constant")
        return f.astype(np.float32)

    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        """Score a PCM16-LE buffer; return (p_done, p_continue)."""
        if len(audio_buffer) < 2:
            return 0.0, 1.0
        usable = audio_buffer[: len(audio_buffer) - len(audio_buffer) % 2]
        audio = np.frombuffer(usable, dtype=np.int16).astype(np.float32) / 32768.0
        feat = self._featurize(audio)
        out = self._session.run(None, {"input_features": feat})
        logit = float(out[0][0, 0])
        p_done = _sigmoid(logit)
        return p_done, 1.0 - p_done
