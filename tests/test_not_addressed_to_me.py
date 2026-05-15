"""Stage 3 contract test — not addressed to agent (v0.1d Task 11).

Success criterion (verbatim):
  pytest tests/test_not_addressed_to_me.py -v passes both sub-cases non-vacuously:
  blocking → silence/NOT_ADDRESSED_TO_AGENT;
  non-blocking → full_response/EOU_CONFIRMED (proving the policy proceeds past social_mode).

Drives fixture not_addressed_to_me_001 directly through SpeakPolicy.decide().
Policy-layer isolation (invariant #5 determinism is testable in isolation).
"""

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness import speak_policy


_FIXTURE = load_fixture("not_addressed_to_me_001")


def _resolution_frame(sub_case: str) -> dict:
    frames = [
        f for f in _FIXTURE["signal_trace"]
        if f.get("sub_case") == sub_case and f.get("resolution_frame") is True
    ]
    assert frames, f"no resolution_frame found for sub_case={sub_case!r}"
    return frames[0]


def _inputs_from_frame(frame: dict) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=frame.get("user_speaking", False),
        eou_probability=frame.get("eou_probability", 0.0),
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=frame.get("user_addressed_agent", False),
        urgency_score=frame.get("urgency_score", 0.0),
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode=frame.get("social_mode", "normal"),
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        audio_visual_conflict_score=frame.get("audio_visual_conflict_score", 0.0),
    )


# ---------------------------------------------------------------------------
# Blocking sub-case: social_mode=user_addressing_other → silence/NOT_ADDRESSED_TO_AGENT

class TestBlockingSubCase:
    """Gate frame-002: social_mode=user_addressing_other in _BLOCKING_SOCIAL_MODES."""

    _frame = _resolution_frame("blocking")
    _inputs = _inputs_from_frame(_resolution_frame("blocking"))

    def test_action_type_is_silence(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.action_type == "silence", (
            f"blocking sub-case must produce silence; got {decision.action_type!r}"
        )

    def test_primary_reason_code_is_not_addressed_to_agent(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT

    def test_determinism(self):
        from dataclasses import astuple
        d1 = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        d2 = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert astuple(d1) == astuple(d2)


# ---------------------------------------------------------------------------
# Non-blocking sub-case: social_mode=user_addressing_agent → full_response/EOU_CONFIRMED
# This is the load-bearing negative: proves the social_mode gate does NOT over-block.

class TestNonBlockingSubCase:
    """Gate frame-102: social_mode=user_addressing_agent NOT in _BLOCKING_SOCIAL_MODES."""

    _frame = _resolution_frame("non_blocking")
    _inputs = _inputs_from_frame(_resolution_frame("non_blocking"))

    def test_action_type_is_full_response(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.action_type == "full_response", (
            f"non-blocking sub-case must proceed past social_mode gate; got {decision.action_type!r}"
        )

    def test_primary_reason_code_is_eou_confirmed(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.primary_reason_code == ReasonCode.EOU_CONFIRMED

    def test_determinism(self):
        from dataclasses import astuple
        d1 = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        d2 = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert astuple(d1) == astuple(d2)
