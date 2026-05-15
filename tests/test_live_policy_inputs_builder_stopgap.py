"""Stopgap test: live builder must default user_addressed_agent=True (Finding 6).

When the production-quality fix lands (see the follow-up issue cited in the
stopgap PR), this test should be replaced with one that exercises the real
addressing-signal path.
"""

from __future__ import annotations

from companion_harness.schemas import TurnSignal
from manual_test_console.live_pipeline import _live_policy_inputs_builder


def test_live_policy_inputs_builder_sets_user_addressed_true() -> None:
    sig = TurnSignal(
        detector="test",
        p_done=0.9,
        p_continue=0.05,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=[],
    )
    inputs = _live_policy_inputs_builder(sig, [sig])
    assert inputs.user_addressed_agent is True
