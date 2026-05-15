"""ASR + lexicon backchannel classifier (BackchannelModel Protocol).

## Why this is not a learned backchannel classifier

There is no widely-available, frame-level, open-source backchannel classifier
comparable to Silero VAD or Pipecat Smart Turn v3. Rather than mock the
detector for manual testing, this module wires a *real* speech model
(faster-whisper tiny) and a curated phrase list together:

  1. Accumulate ~1.5 s of audio per session.
  2. Periodically (every N frames) transcribe the trailing window with
     deterministic settings (`temperature=0`, `beam_size=1`,
     `condition_on_previous_text=False`).
  3. Score 1.0 if the transcript fuzzy-matches a known backchannel phrase,
     else 0.0. Frames that don't trigger a fresh ASR run reuse the last
     transcript-based score until the next decision window.

The output is therefore "real model on real audio" (satisfying the
"no mocked part" requirement for v0.1f manual testing) even though the
classifier head is hand-crafted. Replace with a learned classifier in a
future PR if/when one is identified.

## Determinism (invariant #5)

faster-whisper at `temperature=0.0`, `beam_size=1`, `condition_on_previous_text=False`
is deterministic given the same audio. The lexicon comparison is pure-Python
string ops. The decision-window cadence is based on a frame counter, so
replay with the same frame sequence is bit-identical.

## Per-frame call pattern

The BackchannelClassifier Protocol mandates per-frame invocation. ASR on
every 32 ms frame would be unaffordable, so we run ASR only every N frames
(default 32, ~1 second). Between ASR runs we return the score from the most
recent window. This is the "fall back to running it only on chunk boundaries
every N frames" pattern noted in the integration plan.
"""

from __future__ import annotations

import array
import re
from pathlib import Path
from typing import Any

import numpy as np

__all__ = ["ASRLexiconBackchannelModel", "BACKCHANNEL_PHRASES"]

# Curated backchannel phrase list. All entries are lowercase, punctuation-free.
# Matching is exact-or-substring after lowercasing + punctuation strip.
BACKCHANNEL_PHRASES: tuple[str, ...] = (
    "mm hmm",
    "mmhmm",
    "uh huh",
    "uhhuh",
    "yeah",
    "yep",
    "yup",
    "right",
    "ok",
    "okay",
    "i see",
    "yes",
    "go on",
    "sure",
    "got it",
    "mm",
    "uh",
    "hmm",
)

# 16 kHz PCM16 → 32 ms per 512-sample frame. ASR every 32 frames ≈ 1 s.
_DEFAULT_ASR_INTERVAL_FRAMES = 32
# Trailing audio window scored by ASR: 1.5 s.
_WINDOW_SAMPLES = 16000 * 3 // 2  # 24000

_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")


def _normalize(text: str) -> str:
    return _PUNCT_RE.sub("", text.strip().lower())


def _is_backchannel(text: str) -> bool:
    norm = _normalize(text)
    if not norm:
        return False
    for phrase in BACKCHANNEL_PHRASES:
        if norm == phrase:
            return True
    # Substring match for short transcripts (≤ 3 words) to catch
    # "mm-hmm." → "mm hmm" → match.
    if len(norm.split()) <= 3:
        for phrase in BACKCHANNEL_PHRASES:
            if phrase in norm:
                return True
    return False


class ASRLexiconBackchannelModel:
    """BackchannelModel adapter: faster-whisper tiny + phrase lexicon.

    Lazy-imports faster_whisper at construction so this module can be
    imported on machines without it.

    Stateful: accumulates a rolling PCM16 buffer per session and triggers
    ASR every `asr_interval_frames` calls. Returns the latest cached score
    between ASR runs.
    """

    def __init__(
        self,
        *,
        model_size: str = "tiny",
        asr_interval_frames: int = _DEFAULT_ASR_INTERVAL_FRAMES,
        window_samples: int = _WINDOW_SAMPLES,
        download_root: str | Path | None = None,
    ) -> None:
        from faster_whisper import WhisperModel  # type: ignore

        kwargs: dict[str, Any] = {"device": "cpu", "compute_type": "int8"}
        if download_root is not None:
            kwargs["download_root"] = str(download_root)
        self._model = WhisperModel(model_size, **kwargs)
        self._asr_interval = max(1, asr_interval_frames)
        self._window_samples = window_samples
        # Rolling float32 buffer of recent audio.
        self._buf = np.zeros(0, dtype=np.float32)
        self._frame_count = 0
        self._last_score = 0.0

    def reset_states(self) -> None:
        self._buf = np.zeros(0, dtype=np.float32)
        self._frame_count = 0
        self._last_score = 0.0

    def __call__(self, frame: bytes) -> float:
        """Return p_backchannel in [0, 1]; runs ASR every N frames."""
        if len(frame) < 2:
            return self._last_score
        usable = frame[: len(frame) - len(frame) % 2]
        samples = np.frombuffer(usable, dtype=np.int16).astype(np.float32) / 32768.0
        self._buf = np.concatenate([self._buf, samples])
        if self._buf.shape[0] > self._window_samples:
            self._buf = self._buf[-self._window_samples :]
        self._frame_count += 1

        if self._frame_count % self._asr_interval != 0:
            return self._last_score

        # Skip ASR if the window is mostly silent (RMS < threshold) — saves
        # latency and also gives a deterministic "no speech → not a
        # backchannel" answer.
        rms = float(np.sqrt(np.mean(self._buf * self._buf))) if self._buf.size else 0.0
        if rms < 0.005:
            self._last_score = 0.0
            return self._last_score

        segments, _info = self._model.transcribe(
            self._buf,
            language="en",
            temperature=0.0,
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = " ".join(s.text for s in segments)
        self._last_score = 1.0 if _is_backchannel(text) else 0.0
        return self._last_score
