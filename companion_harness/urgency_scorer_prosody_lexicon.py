"""ProsodyLexiconUrgencyScorer — real urgency scorer for #171 (RFC #238).

Two-signal composite urgency scorer:
  - Lexicon: regex on 13 distress phrases; any match yields 1.0.
  - Prosody: speech-rate (words per second) + RMS energy from 16-bit PCM
    audio, fused into a single 0.0-0.7 score.

Fusion: max(lexicon, prosody), clipped to [0.0, 1.0]. No model load; runs
in-line on the T2 path. Implements the UrgencyScorer Protocol declared in
companion_harness.urgency_scorer.
"""

from __future__ import annotations

import math
import re
import struct

__all__ = ["ProsodyLexiconUrgencyScorer"]

# 13 distress phrases. Word-boundary, case-insensitive.
_DISTRESS_PHRASES: tuple[str, ...] = (
    r"help",
    r"stop",
    r"i can't",
    r"emergency",
    r"hurt",
    r"can't breathe",
    r"need help",
    r"call 911",
    r"too much",
    r"overwhelmed",
    r"panic",
    r"scared",
    r"please stop",
)

# Pre-compile a single alternation regex with word boundaries.
# The apostrophe in "can't" / "i can't" sits inside the token, so we
# anchor each phrase with \b at the start and end of the phrase.
_DISTRESS_RE = re.compile(
    r"(?i)(?:" + "|".join(r"\b" + p + r"\b" for p in _DISTRESS_PHRASES) + r")"
)

_SAMPLE_RATE_HZ = 16000
_RMS_NORM_DIVISOR = 16384.0
_SPEECH_RATE_THRESHOLD_WPS = 3.5
_RMS_NORM_THRESHOLD = 0.5
_PROSODY_COMPONENT_BONUS = 0.3
_PROSODY_MAX = 0.7


class ProsodyLexiconUrgencyScorer:
    """Composite urgency scorer combining lexicon match + prosody features.

    Signature matches the UrgencyScorer Protocol: score(transcript, audio).
    """

    def score(self, transcript: str, audio: bytes | None) -> float:
        if not transcript and not audio:
            return 0.0

        lexicon_score = self._score_lexicon(transcript or "")
        if lexicon_score >= 1.0:
            return 1.0

        prosody_score = self._score_prosody(transcript or "", audio)
        fused = max(lexicon_score, prosody_score)
        if fused < 0.0:
            return 0.0
        if fused > 1.0:
            return 1.0
        return fused

    @staticmethod
    def _score_lexicon(transcript: str) -> float:
        if not transcript:
            return 0.0
        if _DISTRESS_RE.search(transcript):
            return 1.0
        return 0.0

    @staticmethod
    def _score_prosody(transcript: str, audio: bytes | None) -> float:
        if not audio:
            return 0.0
        num_samples = len(audio) // 2
        if num_samples == 0:
            return 0.0

        # Speech rate: words / seconds. seconds = num_samples / sample_rate.
        word_count = len(transcript.split()) if transcript else 0
        duration_s = num_samples / _SAMPLE_RATE_HZ
        speech_rate = word_count / duration_s if duration_s > 0 else 0.0

        # RMS over little-endian signed 16-bit samples.
        samples = struct.unpack(f"<{num_samples}h", audio[: num_samples * 2])
        mean_sq = sum(s * s for s in samples) / num_samples
        rms = math.sqrt(mean_sq)
        rms_norm = min(1.0, rms / _RMS_NORM_DIVISOR)

        rate_hit = speech_rate > _SPEECH_RATE_THRESHOLD_WPS
        rms_hit = rms_norm > _RMS_NORM_THRESHOLD
        if rate_hit and rms_hit:
            return _PROSODY_MAX
        if rate_hit or rms_hit:
            return _PROSODY_COMPONENT_BONUS
        return 0.0
