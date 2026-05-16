"""AddressingClassifier — 3-tier derivation unit tests.

Covers the wake-word / speaker-count / mechanical-fallback rule from the
project-lead direction (post-PR #144 review).

Determinism (invariant #5): every assertion is on the exact bool/string
returned by the classifier; no fuzzy comparisons.
"""

from __future__ import annotations

from companion_harness.addressing_classifier import (
    AddressingSignal,
    WakeWordAddressingClassifier,
    derive_user_addressed_agent,
)


# ---------------------------------------------------------------------------
# Tier 1: explicit (wake-word)
# ---------------------------------------------------------------------------


def test_wake_word_in_transcript_yields_explicit_true() -> None:
    clf = WakeWordAddressingClassifier()
    sig = clf("hey companion what time is it", speaker_count=None, social_mode="user_addressing_other")
    assert sig.confidence == "explicit"
    assert sig.evidence == "wake_word_match:companion"
    assert derive_user_addressed_agent(sig, "user_addressing_other") is True


def test_wake_word_case_insensitive_yields_explicit() -> None:
    clf = WakeWordAddressingClassifier()
    sig = clf("HEY COMPANION!", speaker_count=None, social_mode="user_addressing_other")
    assert sig.confidence == "explicit"
    assert derive_user_addressed_agent(sig, "user_addressing_other") is True


def test_wake_word_with_terminal_punctuation_yields_explicit() -> None:
    """`'companion?'` should still tokenize to `companion` and match."""
    clf = WakeWordAddressingClassifier()
    sig = clf("companion?", speaker_count=None, social_mode="user_addressing_agent")
    assert sig.confidence == "explicit"
    assert sig.evidence == "wake_word_match:companion"


def test_wake_word_as_substring_does_not_match() -> None:
    """Word-boundary discipline: `agentic` MUST NOT match `agent`."""
    clf = WakeWordAddressingClassifier(agent_names=("agent",))
    sig = clf("this is agentic behaviour", speaker_count=None, social_mode="user_addressing_other")
    assert sig.confidence == "implicit"
    assert derive_user_addressed_agent(sig, "user_addressing_other") is False


def test_custom_agent_name_matches() -> None:
    clf = WakeWordAddressingClassifier(agent_names=("aria",))
    sig = clf("aria, what's the weather", speaker_count=None, social_mode="user_addressing_other")
    assert sig.confidence == "explicit"
    assert sig.evidence == "wake_word_match:aria"


# ---------------------------------------------------------------------------
# Tier 2: background (multi-speaker)
# ---------------------------------------------------------------------------


def test_multi_speaker_count_two_yields_background_false() -> None:
    clf = WakeWordAddressingClassifier()
    sig = clf("the weather is nice today", speaker_count=2, social_mode="user_addressing_agent")
    assert sig.confidence == "background"
    assert sig.evidence == "multi_speaker:2"
    assert derive_user_addressed_agent(sig, "user_addressing_agent") is False


def test_multi_speaker_count_three_yields_background() -> None:
    clf = WakeWordAddressingClassifier()
    sig = clf("just chatting", speaker_count=3, social_mode="user_addressing_agent")
    assert sig.confidence == "background"
    assert sig.evidence == "multi_speaker:3"
    assert derive_user_addressed_agent(sig, "user_addressing_agent") is False


# ---------------------------------------------------------------------------
# Tier 3: implicit (mechanical fallback)
# ---------------------------------------------------------------------------


def test_single_speaker_mode_addressing_agent_yields_implicit_true() -> None:
    """Substantive transcript (≥3 tokens, not denylist) ⇒ implicit True."""
    clf = WakeWordAddressingClassifier()
    sig = clf("what time is it", speaker_count=1, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    assert sig.evidence == "implicit_fallback"
    assert derive_user_addressed_agent(sig, "user_addressing_agent", transcript="what time is it") is True


def test_implicit_short_transcript_returns_false() -> None:
    """Short transcript (< IMPLICIT_MIN_TOKENS) ⇒ implicit False regardless of social_mode."""
    clf = WakeWordAddressingClassifier()
    sig = clf("hi", speaker_count=1, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    assert derive_user_addressed_agent(sig, "user_addressing_agent", transcript="hi") is False


def test_speaker_count_none_falls_through_to_implicit() -> None:
    """Unknown speaker count (diarization not wired) ⇒ implicit fallback."""
    clf = WakeWordAddressingClassifier()
    sig = clf("the weather is nice", speaker_count=None, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    assert derive_user_addressed_agent(sig, "user_addressing_agent", transcript="the weather is nice") is True


def test_empty_transcript_speaker_count_none_yields_implicit_false() -> None:
    """Empty transcript ⇒ implicit tier ⇒ False (invariant #8: silence wins ties)."""
    clf = WakeWordAddressingClassifier()
    sig = clf("", speaker_count=None, social_mode="user_addressing_agent")
    assert sig.confidence == "implicit"
    assert derive_user_addressed_agent(sig, "user_addressing_agent", transcript="") is False


def test_empty_transcript_other_mode_yields_implicit_false() -> None:
    clf = WakeWordAddressingClassifier()
    sig = clf("", speaker_count=None, social_mode="background_presence")
    assert sig.confidence == "implicit"
    assert derive_user_addressed_agent(sig, "background_presence", transcript="") is False


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_wake_word_wins_over_multi_speaker() -> None:
    """Explicit beats background — user named the agent regardless of who else is present."""
    clf = WakeWordAddressingClassifier()
    sig = clf("hey companion, what's up", speaker_count=3, social_mode="background_presence")
    assert sig.confidence == "explicit"
    assert sig.evidence == "wake_word_match:companion"
    assert derive_user_addressed_agent(sig, "background_presence") is True


# ---------------------------------------------------------------------------
# Determinism (invariant #5)
# ---------------------------------------------------------------------------


def test_same_inputs_produce_same_output() -> None:
    """Bit-identical output for bit-identical inputs (invariant #5)."""
    clf = WakeWordAddressingClassifier()
    a = clf("hey companion", speaker_count=None, social_mode="user_addressing_agent")
    b = clf("hey companion", speaker_count=None, social_mode="user_addressing_agent")
    assert a == b
    assert isinstance(a, AddressingSignal)
