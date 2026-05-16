"""Spec-aligned test: live builder emits placeholder `user_addressed_agent`;
the orchestrator's `AddressingClassifier` is the authoritative source.

History:
  - PR #140 stopgapped `user_addressed_agent=True` so the harness could speak.
  - PR #142 replaced that with a mechanical derivation from `social_mode`.
  - This PR replaces the mechanical derivation with the 3-tier
    `AddressingClassifier` (wake-word + speaker-count placeholder +
    mechanical fallback). The builder now emits a `False` placeholder; the
    orchestrator overrides it post-ASR via the classifier.

The spec (architecture-v0.1.md:793-803) still defines `privacy_mode`,
`social_mode`, `risk_mode` as first-class enumerated state; this test
keeps the mode-defaults assertion to guard against regressions there.
"""

from __future__ import annotations

from companion_harness.addressing_classifier import (
    WakeWordAddressingClassifier,
    derive_user_addressed_agent,
)
from companion_harness.schemas import TurnSignal
from manual_test_console.live_pipeline import _live_policy_inputs_builder

# Spec-enumerated values from architecture-v0.1.md:793-803.
_SPEC_PRIVACY_MODES = {
    "normal",
    "no_memory",
    "no_camera_memory",
    "local_only",
    "guest_present",
    "child_present",
    "sensitive_conversation",
}
_SPEC_SOCIAL_MODES = {
    "user_addressing_agent",
    "user_addressing_other",
    "group_conversation",
    "background_presence",
}
_SPEC_RISK_MODES = {
    "normal",
    "medical_legal_financial_caution",
    "emotional_distress",
    "crisis",
    "emergency",
}
# `current_task_mode` spec values (architecture-v0.1.md:478-480, 793-803).
_SPEC_TASK_MODES = {
    "normal",
    "creative_focus",
    "cooking",
    "sleep_winddown",
    "walking_outdoor",
    "crisis",
}


def _make_signal() -> TurnSignal:
    return TurnSignal(
        detector="test",
        p_done=0.9,
        p_continue=0.05,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=[],
    )


def test_builder_emits_user_addressed_agent_placeholder_false() -> None:
    """The builder now emits `False` as a placeholder; the orchestrator's
    AddressingClassifier overrides this post-ASR per the 3-tier rule."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    assert inputs.user_addressed_agent is False


def test_mode_field_defaults_are_spec_enumerated() -> None:
    """All four mode-field defaults must be valid spec-enumerated values."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    assert inputs.privacy_mode in _SPEC_PRIVACY_MODES
    assert inputs.current_task_mode in _SPEC_TASK_MODES
    assert inputs.social_mode in _SPEC_SOCIAL_MODES
    assert inputs.risk_mode in _SPEC_RISK_MODES


def test_classifier_returns_false_on_empty_transcript() -> None:
    """Empty transcript ⇒ implicit tier ⇒ False (invariant #8: silence wins ties).
    Replaces the pre-Finding-12 test that incorrectly asserted True for empty
    transcripts when social_mode=user_addressing_agent (solo-operator default)."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    clf = WakeWordAddressingClassifier()
    signal = clf(transcript="", speaker_count=None, social_mode=inputs.social_mode)
    assert derive_user_addressed_agent(signal, inputs.social_mode, transcript="") is False


def test_classifier_returns_false_on_short_transcript() -> None:
    """Short transcript (< IMPLICIT_MIN_TOKENS) ⇒ implicit False regardless of social_mode."""
    clf = WakeWordAddressingClassifier()
    for mode in ("user_addressing_agent", "user_addressing_other", "background_presence"):
        signal = clf(transcript="you", speaker_count=None, social_mode=mode)
        assert derive_user_addressed_agent(signal, mode, transcript="you") is False


def test_classifier_returns_true_on_substantive_transcript() -> None:
    """Substantive transcript (≥3 tokens, not denylist) ⇒ implicit True."""
    clf = WakeWordAddressingClassifier()
    signal = clf(transcript="what is the time", speaker_count=None, social_mode="user_addressing_agent")
    assert derive_user_addressed_agent(signal, "user_addressing_agent", transcript="what is the time") is True
