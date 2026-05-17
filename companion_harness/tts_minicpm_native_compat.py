"""torchaudio compatibility shim for stepaudio2/token2wav.

torchaudio 2.11 in the companion-harness venv delegates load/save to
torchcodec, which is not installed. soundfile handles WAV natively; the
patch is transparent to callers that only read/write 16-bit PCM WAV.

Extracted from tts_minicpm_native.py so both the batch-mode (pre-refactor)
and streaming adapters share one copy.
"""

from __future__ import annotations


def _patch_torchaudio() -> None:
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
