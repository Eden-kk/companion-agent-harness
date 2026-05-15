"""Stage 2 contract test — ambiguous deictic refusal (v0.1c Task 13).

Success criterion (verbatim):
  pytest -k ambiguous_deictic_refusal passes non-vacuously — the ambiguous
  sub-case produces action_type="clarification" + primary_reason_code=DEICTIC_AMBIGUOUS
  (not full_response), AND a positive assertion that the unambiguous
  single-candidate sub-case DOES resolve confidently.

Drives fixture deictic_ambiguity_001 directly through SpeakPolicy with
stub-injected PolicyInputs.  No VisionSidecar or DeicticDetector constructed —
policy-layer isolation (invariant #5 determinism is testable in isolation).
"""

import pytest

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness import speak_policy


# ---------------------------------------------------------------------------
# Persistent fixture + helper (loaded once per module, not re-constructed per test)

_FIXTURE = load_fixture("deictic_ambiguity_001")


def _gate_frame(sub_case: str) -> dict:
    """Return the gate frame for the given sub_case from the fixture signal_trace."""
    frames = [
        f for f in _FIXTURE["signal_trace"]
        if f.get("sub_case") == sub_case and f.get("event_type") == "deictic_classification"
    ]
    assert frames, f"no deictic_classification gate frame found for sub_case={sub_case!r}"
    return frames[0]


def _inputs_from_gate_frame(frame: dict, *, deictic_ambiguous: bool) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=frame.get("scene_change_score", 0.0),
        deictic_reference=True,
        deictic_ambiguous=deictic_ambiguous,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


# ---------------------------------------------------------------------------
# Ambiguous sub-case: two plausible candidates → clarification + DEICTIC_AMBIGUOUS

class TestAmbiguousDeicticCase:
    """Gate frame-003: two candidates (mug 0.72, bottle 0.68); deictic_p=0.91."""

    _frame = _gate_frame("ambiguous")
    _inputs = _inputs_from_gate_frame(_gate_frame("ambiguous"), deictic_ambiguous=True)

    def test_action_type_is_clarification(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.action_type == "clarification", (
            f"ambiguous deictic case must not produce full_response; got {decision.action_type!r}"
        )

    def test_primary_reason_code_is_deictic_ambiguous(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.primary_reason_code == ReasonCode.DEICTIC_AMBIGUOUS

    def test_not_full_response(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.action_type != "full_response"

    def test_determinism(self):
        """Invariant #5: identical inputs → bit-identical decisions."""
        from dataclasses import astuple
        d1 = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        d2 = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert astuple(d1) == astuple(d2)


# ---------------------------------------------------------------------------
# Unambiguous sub-case: single candidate (mug 0.92) → full_response (positive discriminator)

class TestUnambiguousDeicticCase:
    """Gate frame-102: single candidate mug at 0.92; test fails if DEICTIC_AMBIGUOUS returned."""

    _frame = _gate_frame("unambiguous")
    _inputs = _inputs_from_gate_frame(_gate_frame("unambiguous"), deictic_ambiguous=False)

    def test_action_type_is_full_response(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.action_type == "full_response", (
            f"unambiguous deictic case must resolve confidently; got {decision.action_type!r}"
        )

    def test_primary_reason_code_is_eou_confirmed(self):
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.primary_reason_code == ReasonCode.EOU_CONFIRMED

    def test_not_deictic_ambiguous(self):
        """Positive discriminator: the test would fail if DEICTIC_AMBIGUOUS is returned for every frame."""
        decision = speak_policy.decide(self._inputs, signal_event_ids=[self._frame["frame_id"]])
        assert decision.primary_reason_code != ReasonCode.DEICTIC_AMBIGUOUS
