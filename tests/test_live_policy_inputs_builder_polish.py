"""Contract tests for two GPT P0 defaults in `_live_policy_inputs_builder`:

1. `assistant_speaking` is now wired from `AudioOutputController.is_playing`
   via a closure factory `_make_live_policy_inputs_builder(is_playing_fn=...)`.
   Previously hardcoded `False`; per GPT review §6 this masked the
   "agent shouldn't talk over itself" policy state.

2. `grounding_confidence` default is now `0.0` (was `1.0`). Per GPT review §6,
   defaulting to `1.0` SUPPRESSED the `VISUAL_LOW_CONFIDENCE` gate even when
   no grounding model was wired — "no vision" was indistinguishable from
   "vision says confident". `0.0` is forward-safe: once a future visual scene
   scorer sets `deictic_reference=True`, the gate fires correctly without
   any rework. Today `deictic_reference` is hardcoded `False` so the gate is
   not actually reached on the live path — the change is safety, not behavior.

See `manual_test_console/live_pipeline.py:_make_live_policy_inputs_builder`
and `companion_harness/speak_policy.py` (gate 6: `deictic_reference and
grounding_confidence < _GROUNDING_CONFIDENCE_THRESHOLD`).
"""

from __future__ import annotations

from dataclasses import replace

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import TurnSignal
from companion_harness.speak_policy import decide
from manual_test_console.live_pipeline import (
    _live_policy_inputs_builder,
    _make_live_policy_inputs_builder,
)


def _make_signal() -> TurnSignal:
    """A TurnSignal that satisfies the EOU-confirmed branch:
    p_done > p_continue (=> user_speaking False) and eou_probability > 0.5.
    """
    return TurnSignal(
        detector="test",
        p_done=0.9,
        p_continue=0.05,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=["evt_signal_1"],
    )


# ---------------------------------------------------------------------------
# Fix 1: `assistant_speaking` reflects is_playing_fn.
# ---------------------------------------------------------------------------


def test_assistant_speaking_false_when_no_is_playing_fn() -> None:
    """Backward compat: module-level builder (no closure binding) keeps
    `assistant_speaking=False`. Existing tests must not break."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    assert inputs.assistant_speaking is False


def test_assistant_speaking_false_when_callback_returns_false() -> None:
    """Closure factory with a callback that returns False → `assistant_speaking=False`."""
    builder = _make_live_policy_inputs_builder(is_playing_fn=lambda: False)
    sig = _make_signal()
    inputs = builder(sig, [sig])
    assert inputs.assistant_speaking is False


def test_assistant_speaking_true_when_callback_returns_true() -> None:
    """Closure factory with a callback that returns True → `assistant_speaking=True`.
    This is the GPT P0 fix: live AudioOutputController.is_playing flows through."""
    builder = _make_live_policy_inputs_builder(is_playing_fn=lambda: True)
    sig = _make_signal()
    inputs = builder(sig, [sig])
    assert inputs.assistant_speaking is True


def test_assistant_speaking_reflects_dynamic_callback_state() -> None:
    """Each builder call reads the callback fresh (no caching). This is what
    makes the closure track `audio_output.is_playing` over a session lifecycle."""
    state = {"playing": False}
    builder = _make_live_policy_inputs_builder(is_playing_fn=lambda: state["playing"])
    sig = _make_signal()

    inputs_off = builder(sig, [sig])
    assert inputs_off.assistant_speaking is False

    state["playing"] = True
    inputs_on = builder(sig, [sig])
    assert inputs_on.assistant_speaking is True

    state["playing"] = False
    inputs_off_again = builder(sig, [sig])
    assert inputs_off_again.assistant_speaking is False


# ---------------------------------------------------------------------------
# Fix 2: `grounding_confidence` default is `0.0`, not `1.0`.
# ---------------------------------------------------------------------------


def test_grounding_confidence_default_is_zero() -> None:
    """The default is `0.0` (no grounding evidence available) per GPT review §6.
    `1.0` would suppress the `VISUAL_LOW_CONFIDENCE` policy gate even when no
    grounding model is wired."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])
    assert inputs.grounding_confidence == 0.0


def test_visual_low_confidence_gate_does_not_fire_when_deictic_reference_false() -> None:
    """Safety net: even with `grounding_confidence=0.0`, the policy gate does
    NOT fire today because `deictic_reference` is hardcoded `False` in the
    builder. Verifies the new default doesn't silently break the manual-test
    happy path while the visual scene scorer is unwired."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])

    # Confirm the builder's current placeholder state.
    assert inputs.deictic_reference is False
    assert inputs.grounding_confidence == 0.0

    # user_addressed_agent must be True to reach gate 9 (full_response) and
    # confirm that nothing on the path before it fired VISUAL_LOW_CONFIDENCE.
    inputs = replace(inputs, user_addressed_agent=True)
    decision = decide(inputs, signal_event_ids=["evt_signal_1"])
    assert decision.primary_reason_code != ReasonCode.VISUAL_LOW_CONFIDENCE
    assert decision.action_type == "full_response"


def test_visual_low_confidence_gate_fires_when_deictic_reference_true_and_low_grounding() -> None:
    """Forward-safety: once a future scene scorer sets `deictic_reference=True`
    while `grounding_confidence` remains low (e.g. our new 0.0 default), the
    policy gate must fire `VISUAL_LOW_CONFIDENCE` silence. With the OLD default
    of 1.0 this gate would never fire — exactly GPT's concern."""
    sig = _make_signal()
    inputs = _live_policy_inputs_builder(sig, [sig])

    # Simulate the future state where the scene scorer has fired.
    inputs = replace(inputs, deictic_reference=True, user_addressed_agent=True)
    assert inputs.grounding_confidence == 0.0  # below _GROUNDING_CONFIDENCE_THRESHOLD=0.5

    decision = decide(inputs, signal_event_ids=["evt_signal_1"])
    assert decision.action_type == "silence"
    assert decision.primary_reason_code == ReasonCode.VISUAL_LOW_CONFIDENCE
