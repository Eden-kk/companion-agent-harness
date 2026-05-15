"""MiniCPM-o 4.5 concrete implementation of the DuplexModel Protocol.

This module lives on b200 only — it imports torch and transformers.
Import it only when running under the b200 venv (CUDA available).
The ForegroundModel adapter in foreground_model.py stays SDK-free.

Text path (v0.1a latency tests):
    Loads text-only (init_vision=False, init_audio=False, init_tts=False).
    chat(text) → str is the only inference entry point.

Streaming path (Task 2 — as_duplex mode):
    MiniCPMStreamingModel loads with init_audio=True so the audio tower is
    available.  infer_stream() drives MiniCPMODuplex.streaming_prefill() +
    streaming_generate() per 1-second chunk without TTS (generate_audio=False).

    Construction: model.as_duplex(generate_audio=False) is called after
    temporarily patching model.init_tts to a no-op.  torchaudio 2.11 in this
    venv requires libcudart.so.13 (unavailable; only .so.12 is present), so
    stepaudio2 cannot be imported.  Since generate_audio=False means the TTS
    path is never entered at inference time, skipping init_tts is safe.

Usage:
    # text-only (v0.1a):
    from companion_harness.foreground_model_minicpm import MiniCPMDuplexModel
    model = MiniCPMDuplexModel()
    out = model.chat(question_text)

    # streaming/duplex (Task 2):
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    model = MiniCPMStreamingModel()
    # model satisfies StreamingDuplexModel; inject into ForegroundModel(model=model, ...)
"""

from __future__ import annotations

import threading
from typing import AsyncGenerator, AsyncIterator

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

from companion_harness.schemas import ThinkerProposal

__all__ = ["MiniCPMDuplexModel", "MiniCPMStreamingModel"]

_MODEL_ID = "openbmb/MiniCPM-o-4_5"

# 16 kHz PCM16 — 1 second of audio = 16000 int16 samples = 32000 bytes
_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = _SAMPLE_RATE  # 1-second chunks match MiniCPMODuplex default CHUNK_MS=1000


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
            init_vision=False,
            init_audio=False,
            init_tts=False,
        ).eval().cuda()
        self._tokenizer = AutoTokenizer.from_pretrained(_MODEL_ID, trust_remote_code=True)
        # Warm up CUDA kernels with three calls of varying input length so that
        # the JIT cache covers different token-sequence shapes before measurement.
        for _q in ("Ready?", "Is the sky blue?", "What is the color of the sky today?"):
            self.chat(_q, max_new_tokens=4)

    def chat(self, text: str, max_new_tokens: int = 8) -> str:  # 8 tokens fits direct_question_001 closed yes/no answers; revisit if fixture gains open-ended questions
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


class MiniCPMStreamingModel:
    """StreamingDuplexModel backed by MiniCPM-o 4.5 in as_duplex mode.

    Loads with init_audio=True so the audio tower is available for streaming
    prefill.  TTS is disabled (generate_audio=False) — Task 3 will wire audio
    output via a TtsAdapter.

    Construction uses model.as_duplex(generate_audio=False) after temporarily
    replacing model.init_tts with a no-op.  The real init_tts requires
    stepaudio2, which imports torchaudio 2.11; that version needs
    libcudart.so.13 which is absent on this host (only .so.12).  Since
    generate_audio=False means streaming_generate() short-circuits before any
    TTS call, skipping init_tts is safe.

    Implements both DuplexModel (infer) and StreamingDuplexModel (infer_stream).
    """

    def __init__(self) -> None:
        base = AutoModel.from_pretrained(
            _MODEL_ID,
            trust_remote_code=True,
            attn_implementation="sdpa",
            torch_dtype=torch.bfloat16,
            init_vision=False,
            init_audio=True,
            init_tts=False,
        ).eval().cuda()

        # Patch init_tts to a no-op before calling as_duplex so that
        # from_existing_model() does not try to import stepaudio2/torchaudio.
        # generate_audio=False ensures the TTS path is never entered at runtime.
        _orig_init_tts = base.init_tts
        base.init_tts = lambda *a, **kw: None  # type: ignore[method-assign]
        try:
            self._duplex = base.as_duplex(generate_audio=False)
        finally:
            base.init_tts = _orig_init_tts  # type: ignore[method-assign]

        self._lock = threading.Lock()  # duplex state is stateful; one stream at a time

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        """DuplexModel Protocol stub — single-frame path not used for streaming."""
        return None

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
    ) -> AsyncGenerator[ThinkerProposal, None]:
        """Coroutine returning an AsyncGenerator of ThinkerProposal candidates.

        Accumulates PCM16/16kHz audio bytes into 1-second chunks, calls
        streaming_prefill + streaming_generate per chunk, and yields a
        ThinkerProposal whenever the model decides to speak (is_listen=False).
        """
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            duplex = self._duplex
            duplex.prepare(prefix_system_prompt="Streaming Omni Conversation.")

            buf = np.array([], dtype=np.float32)

            def _process_chunk(pcm_float: np.ndarray) -> ThinkerProposal | None:
                duplex.streaming_prefill(audio_waveform=pcm_float)
                result = duplex.streaming_generate(
                    max_new_speak_tokens_per_chunk=duplex.max_new_speak_tokens_per_chunk,
                    temperature=duplex.temperature,
                    top_k=duplex.top_k,
                    top_p=duplex.top_p,
                    listen_prob_scale=duplex.listen_prob_scale,
                    text_repetition_penalty=duplex.text_repetition_penalty,
                    text_repetition_window_size=duplex.text_repetition_window_size,
                )
                text = result.get("text", "")
                if not result.get("is_listen", True) and text:
                    return ThinkerProposal(
                        proposal_type="observation",
                        content=text,
                        trigger="speech",
                        confidence=0.9,
                        novelty=0.5,
                        interruption_cost=0.3,
                        max_utterance_ms=5000,
                        cooldown_consumed="speech_turn",
                        caused_by=list(caused_by),
                    )
                return None

            async for audio_bytes, _video in frame_iter:
                # PCM16 → float32 normalised to [-1, 1]
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                buf = np.concatenate([buf, samples])
                while len(buf) >= _CHUNK_SAMPLES:
                    chunk, buf = buf[:_CHUNK_SAMPLES], buf[_CHUNK_SAMPLES:]
                    proposal = _process_chunk(chunk)
                    if proposal is not None:
                        yield proposal

            # drain any sub-second tail
            if len(buf) > 0:
                # pad to full chunk with silence so streaming_prefill succeeds
                pad = np.zeros(_CHUNK_SAMPLES - len(buf), dtype=np.float32)
                chunk = np.concatenate([buf, pad])
                proposal = _process_chunk(chunk)
                if proposal is not None:
                    yield proposal

        return _gen()
