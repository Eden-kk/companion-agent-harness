"""FasterWhisperASRModel — real ASR via faster-whisper.

Concrete `ASRModel` implementation (see `companion_harness.asr_adapter`).
Imports `faster_whisper` lazily at construction; this module is safe to
import on machines without GPU / without the SDK installed, but
instantiation will fail there.

## Determinism (invariant #5)

- `temperature=0.0`  — greedy decoding, no sampling.
- `beam_size=1`      — no beam-search variance.
- `condition_on_previous_text=False` — no autoregressive variance across
  utterances.
- `language` kwarg   — pass None for whisper auto-detect (Tier-A-only session),
  or a BCP-47 code (e.g. "en", "zh") to pin deterministically.

## Invocation cadence

Called once per EOU (full utterance), NOT per-frame. See ASRModel docstring.
"""

from __future__ import annotations

from typing import Any

__all__ = ["FasterWhisperASRModel"]


class FasterWhisperASRModel:
    """Real ASR via faster-whisper. b200 only — imports faster_whisper.

    Determinism:
    - temperature=0.0 (greedy decoding, no sampling)
    - beam_size=1 (no beam-search variance)
    - condition_on_previous_text=False (no autoregressive variance across utterances)
    - language pinned when provided (None = whisper auto-detect, Tier-A-only)

    Invocation cadence: once per EOU (full utterance), NOT per-frame.
    """

    MODEL_ID = "tiny.en"  # class-level fallback; overridden by model_id kwarg

    def __init__(
        self,
        *,
        model_id: str = "tiny.en",
        language: str | None = None,
        device: str = "cuda",
        compute_type: str = "float16",
    ) -> None:
        from faster_whisper import WhisperModel  # type: ignore  # lazy import

        self._model_id = model_id
        self._language = language
        self._model: Any = WhisperModel(model_id, device=device, compute_type=compute_type)

    def __call__(self, audio_chunks: bytes, sample_rate: int = 16000) -> str:
        import numpy as np  # type: ignore  # lazy import

        # PCM16 LE bytes → float32 normalized [-1, 1]
        samples = np.frombuffer(audio_chunks, dtype=np.int16).astype(np.float32) / 32768.0
        segments, _info = self._model.transcribe(
            samples,
            language=self._language,
            temperature=0.0,
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=False,
            without_timestamps=True,
        )
        return "".join(seg.text for seg in segments).strip()
