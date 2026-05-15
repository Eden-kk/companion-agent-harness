"""v0.1j Task 5: grounding_confidence producer wiring contract tests.

Wires `_make_live_policy_inputs_builder(vision_sidecar=...)` so
`PolicyInputs.grounding_confidence` reads from the per-session
`VisionSidecar.grounding_confidence()` accessor instead of a literal.

The change EXTENDS the existing `_make_live_policy_inputs_builder(is_playing_fn=...)`
factory from PR #155 — the `is_playing_fn` channel for `assistant_speaking` is
preserved (regression-guarded by
`test_assistant_speaking_still_sourced_from_is_playing_fn`).

Success criterion:
    /raid/yid042/venvs/companion-harness/bin/python3 -m pytest \
        tests/test_grounding_confidence_wired.py -v
"""

from __future__ import annotations

import inspect

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs, TurnSignal
from companion_harness.vision_sidecar import (
    FrameRef,
    GroundingModel,
    VisionSidecar,
    _NullGroundingModel,
)
from manual_test_console.live_pipeline import _make_live_policy_inputs_builder


# ---------------------------------------------------------------------------
# Fakes


class _FakeSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0


class _FakeGroundingModel:
    """Returns scripted (label, confidence)."""

    def __init__(self, confidence: float) -> None:
        self._confidence = confidence

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return "object", self._confidence


assert isinstance(_FakeGroundingModel(0.9), GroundingModel)


def _make_sidecar(confidence: float) -> VisionSidecar:
    return VisionSidecar(
        scene_scorer=_FakeSceneScorer(),
        grounding_model=_FakeGroundingModel(confidence),
    )


def _make_signal() -> TurnSignal:
    return TurnSignal(
        detector="test",
        p_done=0.9,
        p_continue=0.05,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=[],
    )


# ---------------------------------------------------------------------------
# Tests


def test_null_grounding_model_returns_zero_with_marker() -> None:
    """_NullGroundingModel returns 0.0 and the source has the UNAVAILABLE marker."""
    model = _NullGroundingModel()
    label, conf = model(b"\x00", "what is this?")
    assert conf == 0.0
    assert label == ""

    src = inspect.getsource(_NullGroundingModel)
    assert "# UNAVAILABLE:" in src


def test_grounding_confidence_uses_vision_sidecar_accessor() -> None:
    """PolicyInputs.grounding_confidence is sourced from vision_sidecar.grounding_confidence(),
    not a hardcoded literal."""
    sidecar = _make_sidecar(0.77)
    # Seed the sidecar's _last_grounding_confidence by calling resolve() with a buffered frame.
    ref = FrameRef(event_id="evt-1", timestamp_mono_ms=1000, frame_bytes=b"\x01")
    sidecar.ingest_frame(ref)
    sidecar.resolve("what is that?", deictic_reference=True)

    builder = _make_live_policy_inputs_builder(vision_sidecar=sidecar)
    sig = _make_signal()
    inputs = builder(sig, [sig])
    assert inputs.grounding_confidence == 0.77


def test_live_builder_no_longer_hardcodes_one() -> None:
    """live_pipeline.py must not contain a literal `grounding_confidence=1.0`."""
    import manual_test_console.live_pipeline as lp_module
    src = inspect.getsource(lp_module)
    assert "grounding_confidence=1.0" not in src


def test_visual_low_confidence_gate_fires_when_deictic_true() -> None:
    """SpeakPolicy returns VISUAL_LOW_CONFIDENCE when deictic_reference=True and
    grounding_confidence < 0.5 (the _GROUNDING_CONFIDENCE_THRESHOLD)."""
    from companion_harness.speak_policy import decide

    inputs = PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=True,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={"full_response": 1},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        grounding_confidence=0.0,
    )
    decision = decide(inputs, signal_event_ids=["evt-1"])
    assert decision.primary_reason_code == ReasonCode.VISUAL_LOW_CONFIDENCE
    assert decision.action_type == "silence"


def test_assistant_speaking_still_sourced_from_is_playing_fn() -> None:
    """Regression guard for PR #155: the Task-5 extension must NOT remove or shadow
    the `is_playing_fn` channel. With a real sidecar bound, `assistant_speaking`
    still reflects `is_playing_fn()` dynamically — not a hardcoded literal.

    If this test ever fails, invariant #4 ("no proactive speech without policy
    approval") has regressed: the policy would lose visibility into whether the
    agent is currently speaking and could approve overlapping speech.
    """
    sidecar = _make_sidecar(0.5)
    state = {"playing": False}
    builder = _make_live_policy_inputs_builder(
        is_playing_fn=lambda: state["playing"],
        vision_sidecar=sidecar,
    )
    sig = _make_signal()

    inputs_off = builder(sig, [sig])
    assert inputs_off.assistant_speaking is False

    state["playing"] = True
    inputs_on = builder(sig, [sig])
    assert inputs_on.assistant_speaking is True
