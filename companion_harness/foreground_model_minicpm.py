"""MiniCPM-o 4.5 concrete implementation of the DuplexModel Protocol.

This module lives on b200 only — it imports torch and transformers.
Import it only when running under the b200 venv (CUDA available).
The ForegroundModel adapter in foreground_model.py stays SDK-free.

Usage:
    from companion_harness.foreground_model_minicpm import MiniCPMDuplexModel
    model = MiniCPMDuplexModel()
    # model is a DuplexModel; inject into ForegroundModel(model=model, ...)

For direct-question latency measurement (text path):
    out = model.chat(question_text)  # returns str
"""

from __future__ import annotations

import os

import torch
from transformers import AutoModel, AutoTokenizer

from companion_harness.schemas import ThinkerProposal

__all__ = ["MiniCPMDuplexModel"]

_MODEL_ID = "openbmb/MiniCPM-o-4_5"


class MiniCPMDuplexModel:
    """DuplexModel backed by MiniCPM-o 4.5 text inference.

    Loads on first instantiation. Callers should reuse a single instance.

    Implements the DuplexModel Protocol: infer(audio_frame) → ThinkerProposal | None.
    Also exposes chat(text) for direct latency measurement without audio framing.
    """

    def __init__(self) -> None:
        self._model = AutoModel.from_pretrained(
            _MODEL_ID,
            trust_remote_code=True,
            attn_implementation="sdpa",
            torch_dtype=torch.bfloat16,
            init_vision=True,
            init_audio=False,
            init_tts=False,
        ).eval().cuda()
        self._tokenizer = AutoTokenizer.from_pretrained(_MODEL_ID, trust_remote_code=True)
        # Warm up CUDA kernels with three calls of varying input length so that
        # the JIT cache covers different token-sequence shapes before measurement.
        for _q in ("Ready?", "Is the sky blue?", "What is the color of the sky today?"):
            self.chat(_q, max_new_tokens=4)

    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        """Send a text question and return the model's text response."""
        with torch.no_grad():
            return self._model.chat(
                msgs=[{"role": "user", "content": text}],
                tokenizer=self._tokenizer,
                max_new_tokens=max_new_tokens,
                generate_audio=False,
                enable_thinking=False,
            )

    def infer(self, audio_frame: bytes) -> ThinkerProposal | None:
        """DuplexModel Protocol: not used in text-path latency tests; returns None."""
        return None
