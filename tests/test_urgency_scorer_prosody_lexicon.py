"""Targeted tests for ProsodyLexiconUrgencyScorer (closes #171 / RFC #238).

Nine CPU-only tests with synthetic 16-bit PCM. No model load.
"""

from __future__ import annotations

import math
import struct

from companion_harness.urgency_scorer import UrgencyScorer
from companion_harness.urgency_scorer_prosody_lexicon import (
    ProsodyLexiconUrgencyScorer,
)

_SAMPLE_RATE_HZ = 16000


def _silence(duration_s: float) -> bytes:
    n = int(_SAMPLE_RATE_HZ * duration_s)
    return struct.pack(f"<{n}h", *([0] * n))


def _loud(duration_s: float, amplitude: int = 20000) -> bytes:
    """Constant +/- amplitude square wave so RMS == amplitude."""
    n = int(_SAMPLE_RATE_HZ * duration_s)
    samples = [amplitude if i % 2 == 0 else -amplitude for i in range(n)]
    return struct.pack(f"<{n}h", *samples)


def test_empty_returns_zero() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    assert scorer.score("", None) == 0.0
    assert scorer.score("", b"") == 0.0


def test_distress_word_help() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    assert scorer.score("help me please", None) == 1.0


def test_distress_phrase_cant_breathe() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    assert scorer.score("i can't breathe", None) == 1.0


def test_distress_with_capitalization() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    assert scorer.score("EMERGENCY!", None) == 1.0
    assert scorer.score("Please STOP", None) == 1.0


def test_neutral_text_low_score() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    # Neutral text + long silence -> 0.0 (no lexicon, low rate, no energy).
    # 5 words / 3 s = 1.67 wps (< 3.5); silence -> rms_norm = 0.
    assert scorer.score("the weather is nice today", _silence(3.0)) == 0.0


def test_high_speech_rate_high_energy() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    # 0.5 s of loud audio (8000 samples) + 5 neutral words -> 10 wps.
    audio = _loud(0.5, amplitude=20000)  # RMS=20000, rms_norm=min(1, 20000/16384)=1.0
    transcript = "the quick brown fox jumps"  # 5 words / 0.5 s = 10 wps > 3.5
    assert scorer.score(transcript, audio) == 0.7


def test_high_energy_only() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    # Loud audio, but only 1 word over 1 s -> 1 wps (below 3.5).
    audio = _loud(1.0, amplitude=20000)
    score = scorer.score("hello", audio)
    assert math.isclose(score, 0.3, rel_tol=1e-9)


def test_high_rate_only() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    # Quiet audio (RMS ~0), but lots of words.
    audio = _silence(0.5)  # 0.5 s
    transcript = "one two three four five six seven"  # 7 / 0.5 = 14 wps
    score = scorer.score(transcript, audio)
    assert math.isclose(score, 0.3, rel_tol=1e-9)


def test_satisfies_protocol() -> None:
    scorer = ProsodyLexiconUrgencyScorer()
    assert isinstance(scorer, UrgencyScorer)
