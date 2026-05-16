"""MiniCPMNativeTtsAdapter — TtsAdapter backed by MiniCPM-o native TTS.

Uses MiniCPMO.chat(generate_audio=True, use_tts_template=True) to convert
text to audio via the model's built-in TTS path.

The base model must have been loaded with init_tts=True (post-PR #197).
This venv's torchaudio.load/save are broken (missing torchcodec); we patch
them to use soundfile so stepaudio2.token2wav can load the reference WAV.

Output format: PCM16 little-endian at 24 kHz mono — same as KokoroTtsAdapter.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections.abc import AsyncIterator

import numpy as np

_log = logging.getLogger(__name__)

__all__ = ["MiniCPMNativeTtsAdapter"]

_SAMPLE_RATE = 24000


def _patch_torchaudio() -> None:
    """Replace torchaudio.load/save with soundfile equivalents.

    torchaudio 2.11 in this venv delegates load/save to torchcodec, which is
    not installed.  soundfile handles WAV natively and is installed; the patch
    is transparent to callers that only read/write 16-bit PCM WAV.
    """
    import torchaudio
    if getattr(torchaudio, "_sf_patched", False):
        return
    import soundfile as sf
    import torch

    def _load(path, backend=None, **kw):
        audio, sr = sf.read(str(path), dtype="float32")
        if audio.ndim == 1:
            audio = audio[None]
        else:
            audio = audio.T
        return torch.from_numpy(audio.copy()), sr

    def _save(path, tensor, sr=None, sample_rate=None, format=None, **kw):
        rate = sr if sr is not None else sample_rate
        data = tensor.squeeze(0).cpu().numpy()
        fmt = format or "WAV"
        sf.write(path, data, rate, format=fmt)

    torchaudio.load = _load
    torchaudio.save = _save
    torchaudio._sf_patched = True


class MiniCPMNativeTtsAdapter:
    """TtsAdapter backed by MiniCPM-o native TTS path.

    Takes a MiniCPMStreamingModel whose base model has init_tts=True.
    Accesses the underlying MiniCPMO instance from the duplex wrapper.

    synthesize():
      1. Pre-initializes token2wav.cache with a silent reference WAV so
         stepaudio2 does not call torchaudio.load(None) during inference.
      2. Calls chat(generate_audio=True, use_tts_template=True) which writes
         a WAV to a tempfile via soundfile.write.
      3. Reads the WAV back with soundfile.read and yields PCM16 bytes.
    """

    def __init__(self, streaming_model: object) -> None:
        _patch_torchaudio()
        # _duplex.model is the base MiniCPMO instance (has .chat() + .tts)
        self._model = streaming_model._duplex.model  # type: ignore[attr-defined]
        self._tokenizer = streaming_model._duplex.tokenizer  # type: ignore[attr-defined]
        # Pre-warm token2wav.cache with a silent reference so every chat() call
        # finds cache non-None and skips _prepare_prompt(None).
        self._prime_token2wav_cache()

    def _prime_token2wav_cache(self) -> None:
        import soundfile as sf  # noqa: WPS433
        silence = np.zeros(16000, dtype=np.float32)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            path = f.name
        try:
            sf.write(path, silence, 16000)
            audio_tok = self._model.tts.audio_tokenizer
            audio_tok.cache = audio_tok._prepare_prompt(path)
        finally:
            if os.path.exists(path):
                os.unlink(path)

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        model = self._model
        tokenizer = self._tokenizer

        def _synthesize_sync() -> bytes:
            import soundfile as sf  # noqa: WPS433

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp_path = tmp.name

            model.chat(
                msgs=[{"role": "user", "content": text}],
                tokenizer=tokenizer,
                generate_audio=True,
                use_tts_template=True,
                output_audio_path=tmp_path,
                max_new_tokens=256,
                enable_thinking=False,
            )
            try:
                wav_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
                if wav_size == 0:
                    # model.chat() swallows TTS errors internally; an empty (or
                    # missing) WAV file means synthesis failed silently.
                    _log.error(
                        "MiniCPMNativeTtsAdapter: chat() returned but WAV file is "
                        "empty — TTS synthesis failed internally (check stderr for "
                        "traceback from model.chat). text=%r", text[:80]
                    )
                    return b""
                samples, _sr = sf.read(tmp_path, dtype="float32")
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            clipped = np.clip(samples, -1.0, 1.0)
            return (clipped * 32767.0).astype("<i2").tobytes()

        pcm_bytes = await loop.run_in_executor(None, _synthesize_sync)
        if pcm_bytes:
            yield pcm_bytes

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE
