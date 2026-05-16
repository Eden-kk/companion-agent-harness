"""Tier-3 solo-fallback gate — Finding 12 regression tests.

Invariant #8: silence wins ties. When the addressing classifier falls through
to the implicit tier, `derive_user_addressed_agent` must return False unless
the transcript provides positive evidence: ≥ IMPLICIT_MIN_TOKENS tokens AND
not in WHISPER_HALLUCINATION_DENYLIST.

Root cause closed: prior to this fix, `derive_user_addressed_agent` returned
`social_mode == "user_addressing_agent"` for the implicit tier. In a single-
operator rig (the only configuration used in manual testing) the orchestrator
sets `social_mode="user_addressing_agent"` as a hardcoded default, so every
transcript — including whisper-tiny hallucinations on silence/noise — reached
the policy layer with `user_addressed_agent=True`, producing `full_response`
on every EOU regardless of whether the user actually spoke.

These tests ensure the gate cannot regress:
  - empty transcript → False
  - short hallucinations → False (parametrized over denylist)
  - substantive transcript → True
  - tier-1 wake-word path unaffected
  - tier-2 diarization path unaffected
"""

from __future__ import annotations

import pytest

from companion_harness.addressing_classifier import (
    IMPLICIT_MIN_TOKENS,
    WHISPER_HALLUCINATION_DENYLIST,
    AddressingSignal,
    WakeWordAddressingClassifier,
    derive_user_addressed_agent,
)

_clf = WakeWordAddressingClassifier()


# ---------------------------------------------------------------------------
# Tier-3 gate: empty transcript
# ---------------------------------------------------------------------------


def test_tier3_solo_with_empty_transcript_returns_false() -> None:
    """Empty transcript ⇒ implicit tier ⇒ False (invariant #8)."""
    sig = _clf("", speaker_count=None, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    result = derive_user_addressed_agent(sig, "user_addressing_agent", transcript="")
    assert result is False


# ---------------------------------------------------------------------------
# Tier-3 gate: short transcripts below token threshold
# ---------------------------------------------------------------------------


def test_tier3_solo_with_short_hallucination_returns_false() -> None:
    """Short single-word transcript ⇒ implicit False (token count < IMPLICIT_MIN_TOKENS)."""
    sig = _clf("you", speaker_count=None, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    result = derive_user_addressed_agent(sig, "user_addressing_agent", transcript="you")
    assert result is False


def test_implicit_min_tokens_boundary() -> None:
    """Exactly IMPLICIT_MIN_TOKENS tokens (no denylist) ⇒ True; one fewer ⇒ False."""
    # Build a transcript of exactly IMPLICIT_MIN_TOKENS tokens not on the denylist.
    enough = " ".join(["word"] * IMPLICIT_MIN_TOKENS)
    one_short = " ".join(["word"] * (IMPLICIT_MIN_TOKENS - 1))

    sig_enough = _clf(enough, speaker_count=None, social_mode="user_addressing_agent")
    sig_short = _clf(one_short, speaker_count=None, social_mode="user_addressing_agent")

    assert derive_user_addressed_agent(sig_enough, "user_addressing_agent", transcript=enough) is True
    assert derive_user_addressed_agent(sig_short, "user_addressing_agent", transcript=one_short) is False


# ---------------------------------------------------------------------------
# Tier-3 gate: denylist phrases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrase", sorted(WHISPER_HALLUCINATION_DENYLIST))
def test_tier3_denylist_phrases_rejected(phrase: str) -> None:
    """Every phrase in WHISPER_HALLUCINATION_DENYLIST ⇒ implicit False."""
    sig = AddressingSignal(confidence="implicit", evidence="implicit_fallback")
    result = derive_user_addressed_agent(sig, "user_addressing_agent", transcript=phrase)
    assert result is False, f"denylist phrase {phrase!r} should yield False"


# ---------------------------------------------------------------------------
# Tier-3 gate: substantive transcript
# ---------------------------------------------------------------------------


def test_tier3_solo_with_substantive_transcript_returns_true() -> None:
    """Substantive transcript (≥ IMPLICIT_MIN_TOKENS, not denylist) ⇒ implicit True."""
    transcript = "what is the time"
    sig = _clf(transcript, speaker_count=None, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    result = derive_user_addressed_agent(sig, "user_addressing_agent", transcript=transcript)
    assert result is True


def test_tier3_substantive_transcript_with_punctuation_returns_true() -> None:
    """Punctuation stripping must not reduce a substantive transcript below threshold."""
    transcript = "what time is it?"
    sig = _clf(transcript, speaker_count=None, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    result = derive_user_addressed_agent(sig, "user_addressing_agent", transcript=transcript)
    assert result is True


# ---------------------------------------------------------------------------
# Tier-1 wake-word: unaffected
# ---------------------------------------------------------------------------


def test_tier1_wake_word_still_works() -> None:
    """Wake-word path is unaffected by tier-3 changes."""
    sig = _clf("hey companion what time is it", speaker_count=None, social_mode="user_addressing_other")
    assert sig.confidence == "explicit"
    assert derive_user_addressed_agent(sig, "user_addressing_other", transcript="hey companion what time is it") is True


def test_tier1_wake_word_with_empty_transcript_param_still_true() -> None:
    """Explicit tier ignores transcript — even empty transcript returns True."""
    sig = AddressingSignal(confidence="explicit", evidence="wake_word_match:companion")
    assert derive_user_addressed_agent(sig, "user_addressing_other", transcript="") is True


# ---------------------------------------------------------------------------
# Tier-2 diarization: unaffected
# ---------------------------------------------------------------------------


def test_tier2_diarization_still_works() -> None:
    """Multi-speaker path is unaffected by tier-3 changes."""
    sig = _clf("the weather is nice today", speaker_count=2, social_mode="user_addressing_agent")
    assert sig.confidence == "background"
    assert derive_user_addressed_agent(sig, "user_addressing_agent", transcript="the weather is nice today") is False


# ---------------------------------------------------------------------------
# Invariant #8: silence wins ties when signal is ambiguous
# ---------------------------------------------------------------------------


def test_invariant_8_silence_wins_ties_when_signal_ambiguous() -> None:
    """For any ambiguous (implicit) signal, False is the default when transcript
    provides no positive evidence (invariant #8)."""
    ambiguous_transcripts = ["", "uh", "um", "hmm", "you", "bye"]
    for t in ambiguous_transcripts:
        sig = AddressingSignal(confidence="implicit", evidence="implicit_fallback")
        result = derive_user_addressed_agent(sig, "user_addressing_agent", transcript=t)
        assert result is False, (
            f"Invariant #8 violated: ambiguous transcript {t!r} yielded True"
        )
