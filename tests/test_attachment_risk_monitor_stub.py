"""AttachmentRiskMonitor stub — unit tests (v0.1g Task 4).

Success criterion: Protocol is runtime-checkable; _NullAttachmentRiskMonitor
returns None for any event stream; Anchor 2 sub-categories are exactly 3.
"""

from companion_harness.attachment_risk_monitor import (
    ATTACHMENT_RISK_SUB_CATEGORIES,
    AttachmentRiskMonitor,
    _NullAttachmentRiskMonitor,
)
from companion_harness.schemas import Event


def _make_event(event_id: str) -> Event:
    return Event(
        event_id=event_id,
        session_id="test",
        schema_version="0.1",
        seq_no=0,
        event_type="test_event",
        timestamp_mono_ms=0,
        timestamp_wall="2026-05-15T00:00:00+00:00",
        source="test",
        caused_by=[],
        payload_hash="abc123",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )


def test_attachment_risk_monitor_protocol_runtime_checkable():
    monitor = _NullAttachmentRiskMonitor()
    assert isinstance(monitor, AttachmentRiskMonitor)


def test_null_attachment_risk_monitor_returns_none_for_empty_stream():
    monitor = _NullAttachmentRiskMonitor()
    assert monitor.assess(iter([])) is None


def test_null_attachment_risk_monitor_returns_none_for_arbitrary_stream():
    monitor = _NullAttachmentRiskMonitor()
    events = [_make_event(f"ev-{i}") for i in range(5)]
    assert monitor.assess(iter(events)) is None


def test_attachment_risk_signal_sub_categories():
    assert len(ATTACHMENT_RISK_SUB_CATEGORIES) == 3
    assert "over_reliance" in ATTACHMENT_RISK_SUB_CATEGORIES
    assert "parasocial_pattern" in ATTACHMENT_RISK_SUB_CATEGORIES
    assert "declining_mood_after_use" in ATTACHMENT_RISK_SUB_CATEGORIES
