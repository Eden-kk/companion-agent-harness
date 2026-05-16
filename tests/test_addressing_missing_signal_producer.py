"""F1 diagnosability: MISSING_SIGNAL_PRODUCER when addressing signal is None.

When ASR is disabled the addressing classifier receives an empty transcript
and may return None.  The policy layer must emit MISSING_SIGNAL_PRODUCER (not
NOT_ADDRESSED_TO_AGENT) so operators can distinguish "no signal at all" from
"user clearly not addressing agent".
"""

import pytest

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness.speak_policy import decide


def _inputs(**overrides) -> PolicyInputs:
    defaults = dict(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=None,  # no addressing signal produced
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )
    defaults.update(overrides)
    return PolicyInputs(**defaults)


def test_none_addressing_emits_missing_signal_producer():
    """user_addressed_agent=None → silence with MISSING_SIGNAL_PRODUCER."""
    result = decide(_inputs(), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.MISSING_SIGNAL_PRODUCER


def test_false_addressing_emits_not_addressed():
    """user_addressed_agent=False → silence with NOT_ADDRESSED_TO_AGENT (existing)."""
    result = decide(_inputs(user_addressed_agent=False), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.NOT_ADDRESSED_TO_AGENT


def test_missing_signal_producer_carries_caused_by():
    """MISSING_SIGNAL_PRODUCER decision inherits the signal_event_ids."""
    result = decide(_inputs(), ["evt-asr-disabled"])
    assert result.caused_by == ["evt-asr-disabled"]


def test_missing_signal_producer_is_deterministic():
    """Same inputs → bit-identical decision (invariant #5)."""
    inputs = _inputs()
    d1 = decide(inputs, ["sig-1"])
    d2 = decide(inputs, ["sig-1"])
    assert d1 == d2
