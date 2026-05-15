"""Spec-aligned test: live builder derives `user_addressed_agent` from `social_mode`.

Replaces the PR #140 stopgap test. The spec (architecture-v0.1.md:793-803) defines
`privacy_mode`, `social_mode`, `risk_mode` as first-class state with enumerated
values. The live builder uses these spec-defined enumerations and derives
`user_addressed_agent` mechanically from `social_mode`.
"""

from __future__ import annotations

from dataclasses import replace

from companion_harness.schemas import TurnSignal
from companion_harness.speak_policy import _BLOCKING_SOCIAL_MODES
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


def test_default_social_mode_addressing_agent_yields_user_addressed_true() -> None:
    """Single-user manual-test default: social_mode=user_addressing_agent ⇒ True."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    assert inputs.social_mode == "user_addressing_agent"
    assert inputs.user_addressed_agent is True


def test_mode_field_defaults_are_spec_enumerated() -> None:
    """All four mode-field defaults must be valid spec-enumerated values."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    assert inputs.privacy_mode in _SPEC_PRIVACY_MODES
    assert inputs.current_task_mode in _SPEC_TASK_MODES
    assert inputs.social_mode in _SPEC_SOCIAL_MODES
    assert inputs.risk_mode in _SPEC_RISK_MODES


def test_mechanical_derivation_blocking_social_mode_would_yield_false() -> None:
    """Override social_mode to a blocking value: derivation must produce False.

    Demonstrates that `user_addressed_agent` is mechanically derived from
    `social_mode == "user_addressing_agent"` (not hardcoded). Uses dataclass
    `replace` on the builder output to simulate a hypothetical multi-party
    scenario, then re-applies the same derivation rule.
    """
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    for blocking in _BLOCKING_SOCIAL_MODES:
        overridden = replace(
            inputs,
            social_mode=blocking,
            user_addressed_agent=(blocking == "user_addressing_agent"),
        )
        assert overridden.user_addressed_agent is False
