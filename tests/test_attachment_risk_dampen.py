"""v0.1g Task 14: attachment-risk dampen contract test.

Success criterion: EventStreamAttachmentRiskMonitor detects high risk;
SpeakPolicy returns silence + ATTACHMENT_RISK_DAMPEN for proactive speech
attempts when attachment_risk_level >= dampen threshold (0.5).
Spec line 855: "do NOT increase proactivity during distress."
"""

from __future__ import annotations

from companion_harness.attachment_risk_monitor import (
    OVER_RELIANCE_COUNT_THRESHOLD,
    EventStreamAttachmentRiskMonitor,
)
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, PolicyInputs
from companion_harness.speak_policy import decide


def _make_event(
    event_id: str,
    event_type: str = "test_event",
    timestamp_mono_ms: int = 0,
    payload_inline: dict | None = None,
) -> Event:
    return Event(
        event_id=event_id,
        session_id="test",
        schema_version="0.1",
        seq_no=0,
        event_type=event_type,
        timestamp_mono_ms=timestamp_mono_ms,
        timestamp_wall="2026-05-15T00:00:00+00:00",
        source="test",
        caused_by=[],
        payload_hash="abc123",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
        payload_inline=payload_inline,
    )


def _proactive_inputs(attachment_risk_level: float, aesthetic: bool = True) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=attachment_risk_level,
        aesthetic_novelty_score=0.8 if aesthetic else 0.0,
        short_response_appropriate=not aesthetic,
    )


def _high_risk_stream(extra: int = 2) -> list[Event]:
    count = OVER_RELIANCE_COUNT_THRESHOLD + extra
    return [
        _make_event(
            f"ev-{i}",
            event_type="policy_decision",
            timestamp_mono_ms=i * 1000,
            payload_inline={"action_type": "full_response"},
        )
        for i in range(count)
    ]


def test_monitor_detects_high_risk():
    """EventStreamAttachmentRiskMonitor returns a signal on an over-reliance stream."""
    monitor = EventStreamAttachmentRiskMonitor()
    result = monitor.assess(iter(_high_risk_stream()))
    assert result is not None
    assert result.confidence > 0.0


def test_attachment_risk_dampens_aesthetic_reaction():
    """attachment_risk_level >= 0.5 → silence + ATTACHMENT_RISK_DAMPEN for aesthetic attempt."""
    result = decide(_proactive_inputs(attachment_risk_level=0.5, aesthetic=True), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.ATTACHMENT_RISK_DAMPEN


def test_attachment_risk_dampens_short_reaction():
    """attachment_risk_level >= 0.5 → silence + ATTACHMENT_RISK_DAMPEN for short_reaction attempt."""
    result = decide(_proactive_inputs(attachment_risk_level=0.5, aesthetic=False), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.ATTACHMENT_RISK_DAMPEN


def test_attachment_risk_below_threshold_does_not_dampen():
    """attachment_risk_level < 0.5 → dampen gate not triggered; aesthetic proceeds."""
    result = decide(_proactive_inputs(attachment_risk_level=0.49, aesthetic=True), ["sig-1"])
    assert result.action_type == "aesthetic_reaction"
    assert result.primary_reason_code == ReasonCode.PROACTIVITY_BUDGET_AVAILABLE


def test_attachment_risk_does_not_block_user_addressed():
    """attachment_risk_level >= 0.5 does NOT block full_response when user addressed agent."""
    inputs = PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.9,
        aesthetic_novelty_score=0.0,
    )
    result = decide(inputs, ["sig-1"])
    assert result.action_type == "full_response"


def test_attachment_risk_high_confidence_from_monitor_integration():
    """End-to-end: monitor detects risk; orchestrator maps confidence → policy dampens."""
    monitor = EventStreamAttachmentRiskMonitor()
    # extra=10: 10 events above threshold → confidence = 10/10 = 1.0
    signal = monitor.assess(iter(_high_risk_stream(extra=OVER_RELIANCE_COUNT_THRESHOLD)))
    assert signal is not None
    assert signal.confidence >= 0.5

    # Orchestrator would map signal.confidence to attachment_risk_level.
    attachment_risk_level = signal.confidence
    result = decide(_proactive_inputs(attachment_risk_level=attachment_risk_level), ["sig-1"])
    assert result.action_type == "silence"
    assert result.primary_reason_code == ReasonCode.ATTACHMENT_RISK_DAMPEN
