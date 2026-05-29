"""DeepInfraWhisperASRModel — ASR via DeepInfra's hosted Whisper large-v3.

Concrete `ASRModel` implementation (see `companion_harness.asr_adapter`).
Offloads transcription to DeepInfra's OpenAI-compatible endpoint so no GPU is
spent locally on ASR — this frees the foreground GPU for the MiniCPM proposer.

## API

POST https://api.deepinfra.com/v1/openai/audio/transcriptions
  Authorization: Bearer $DEEPINFRA_API_KEY
  multipart: model=openai/whisper-large-v3, file=<wav>, temperature=0
  -> {"text": "..."}

The API key is read from the DEEPINFRA_API_KEY env var (never hardcoded /
committed). Construction fails fast if it is missing.

## Determinism (invariant #5)

`temperature=0` is sent for greedy decoding, but this is a *remote* model:
unlike `FasterWhisperASRModel` it is best-effort deterministic, not
bit-identical, and not suitable for Tier-B replay. It is used for the
continuous live path's advisory USER-dialogue transcript only.

## Invocation cadence / blocking

Called once per EOU (full utterance) from the ASR sidecar's
ThreadPoolExecutor — a blocking network round-trip here is off the realtime
event loop (invariant #10). Network/HTTP errors are swallowed and return ""
so a transient API failure degrades to "no transcript", never crashes the loop.
"""

from __future__ import annotations

import io
import os
import wave
from typing import Any

__all__ = ["DeepInfraWhisperASRModel"]

_ENDPOINT = "https://api.deepinfra.com/v1/openai/audio/transcriptions"


class DeepInfraWhisperASRModel:
    """Real ASR via DeepInfra hosted Whisper. No local GPU/SDK required."""

    def __init__(
        self,
        *,
        model_id: str = "openai/whisper-large-v3",
        language: str | None = None,
        api_key: str | None = None,
        endpoint: str = _ENDPOINT,
        timeout: float = 30.0,
    ) -> None:
        key = api_key or os.environ.get("DEEPINFRA_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPINFRA_API_KEY not set; required for DeepInfraWhisperASRModel"
            )
        self._key = key
        self._model_id = model_id
        self._language = language
        self._endpoint = endpoint
        self._timeout = timeout

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        import requests  # lazy import

        if not audio_chunks:
            return ""

        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)  # PCM16
            w.setframerate(sample_rate)
            w.writeframes(audio_chunks)
        buf.seek(0)

        data: dict[str, Any] = {"model": self._model_id, "temperature": "0"}
        if self._language:
            data["language"] = self._language
        try:
            resp = requests.post(
                self._endpoint,
                headers={"Authorization": f"Bearer {self._key}"},
                data=data,
                files={"file": ("utterance.wav", buf, "audio/wav")},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return (resp.json().get("text") or "").strip()
        except Exception:
            # Non-fatal: degrade to empty transcript, never break the realtime loop.
            return ""
