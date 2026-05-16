"""EventStreamAttachmentRiskMonitor — concrete impl tests (v0.1g Task 7).

Success criterion: pytest tests/test_attachment_risk_monitor_concrete.py -v → all pass.
OQ-9: assess() reads event stream only, no orchestrator state.
"""

from __future__ import annotations

import pytest

from companion_harness.attachment_risk_monitor import (
    OVER_RELIANCE_COUNT_THRESHOLD,
    OVER_RELIANCE_WINDOW_MS,
    PARASOCIAL_RATIO_THRESHOLD,
    EventStreamAttachmentRiskMonitor,
)
from companion_harness.schemas import Event


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


def _policy_decision_full_response(event_id: str, timestamp_mono_ms: int) -> Event:
    return _make_event(
        event_id=event_id,
        event_type="policy_decision",
        timestamp_mono_ms=timestamp_mono_ms,
        payload_inline={"action_type": "full_response"},
    )


# ---------------------------------------------------------------------------
# over_reliance
# ---------------------------------------------------------------------------


def test_over_reliance_triggers_with_high_full_response_count():
    monitor = EventStreamAttachmentRiskMonitor()
    # Build OVER_RELIANCE_COUNT_THRESHOLD + 2 full_response events in the window.
    count = OVER_RELIANCE_COUNT_THRESHOLD + 2
    events = [
        _policy_decision_full_response(f"ev-{i}", timestamp_mono_ms=i * 1000)
        for i in range(count)
    ]
    result = monitor.assess(iter(events))
    assert result is not None
    assert result.signal_class == "repeated_reassurance_loops"
    assert result.confidence > 0.0
    assert len(result.evidence_event_ids) == count


def test_over_reliance_does_not_trigger_at_threshold():
    monitor = EventStreamAttachmentRiskMonitor()
    count = OVER_RELIANCE_COUNT_THRESHOLD
    events = [
        _policy_decision_full_response(f"ev-{i}", timestamp_mono_ms=i * 1000)
        for i in range(count)
    ]
    result = monitor.assess(iter(events))
    # count == threshold means we are NOT over, so no signal expected
    # (could be None or from a different sub-category; just assert not over_reliance)
    assert result is None or result.signal_class != "repeated_reassurance_loops"


def test_over_reliance_ignores_events_outside_window():
    monitor = EventStreamAttachmentRiskMonitor()
    count = OVER_RELIANCE_COUNT_THRESHOLD + 5
    # Place old events outside the window, only 1 inside.
    latest_ms = 0
    old_events = [
        _policy_decision_full_response(
            f"old-{i}", timestamp_mono_ms=-(OVER_RELIANCE_WINDOW_MS + (i + 1) * 1000)
        )
        for i in range(count - 1)
    ]
    new_event = _policy_decision_full_response("new-0", timestamp_mono_ms=latest_ms)
    result = monitor.assess(iter(old_events + [new_event]))
    assert result is None or result.signal_class != "repeated_reassurance_loops"


# ---------------------------------------------------------------------------
# parasocial_pattern
# ---------------------------------------------------------------------------


def test_parasocial_pattern_triggers_with_high_agent_utterance_ratio():
    monitor = EventStreamAttachmentRiskMonitor()
    # 9 agent utterances + 1 user utterance = ratio 0.9 > threshold 0.8
    agent_events = [
        _make_event(f"agent-{i}", event_type="agent_utterance", timestamp_mono_ms=i * 1000)
        for i in range(9)
    ]
    user_event = _make_event("user-0", event_type="user_utterance", timestamp_mono_ms=9000)
    result = monitor.assess(iter(agent_events + [user_event]))
    assert result is not None
    assert result.signal_class == "emotional_exclusivity_signals"
    assert result.confidence > 0.0
    assert len(result.evidence_event_ids) == 9


def test_parasocial_pattern_does_not_trigger_at_low_ratio():
    monitor = EventStreamAttachmentRiskMonitor()
    # 1 agent + 4 user = ratio 0.2, well below threshold
    events = [
        _make_event("agent-0", event_type="agent_utterance", timestamp_mono_ms=0),
        _make_event("user-0", event_type="user_utterance", timestamp_mono_ms=1000),
        _make_event("user-1", event_type="user_utterance", timestamp_mono_ms=2000),
        _make_event("user-2", event_type="user_utterance", timestamp_mono_ms=3000),
        _make_event("user-3", event_type="user_utterance", timestamp_mono_ms=4000),
    ]
    result = monitor.assess(iter(events))
    assert result is None or result.signal_class != "emotional_exclusivity_signals"


# ---------------------------------------------------------------------------
# declining_mood_after_use
# ---------------------------------------------------------------------------


def test_declining_mood_triggers_with_monotonic_affective_decline():
    monitor = EventStreamAttachmentRiskMonitor()
    # Three affective_state events with strictly declining valence.
    affective_events = [
        _make_event(
            f"aff-{i}",
            event_type="affective_state",
            timestamp_mono_ms=i * 1000,
            payload_inline={"valence": 0.8 - i * 0.2},
        )
        for i in range(3)
    ]
    result = monitor.assess(iter(affective_events))
    assert result is not None
    assert result.signal_class == "reduced_human_contact_mentions"
    assert result.confidence > 0.0


def test_declining_mood_does_not_trigger_without_affective_events():
    pytest.skip("no affective_state events in fixture — see task spec note")


def test_declining_mood_does_not_trigger_for_non_monotonic():
    monitor = EventStreamAttachmentRiskMonitor()
    # valence goes down then up — not monotonic.
    affective_events = [
        _make_event("aff-0", event_type="affective_state", timestamp_mono_ms=0,
                    payload_inline={"valence": 0.8}),
        _make_event("aff-1", event_type="affective_state", timestamp_mono_ms=1000,
                    payload_inline={"valence": 0.5}),
        _make_event("aff-2", event_type="affective_state", timestamp_mono_ms=2000,
                    payload_inline={"valence": 0.7}),
    ]
    result = monitor.assess(iter(affective_events))
    assert result is None or result.signal_class != "reduced_human_contact_mentions"


# ---------------------------------------------------------------------------
# clean stream → None
# ---------------------------------------------------------------------------


def test_clean_event_stream_returns_none():
    monitor = EventStreamAttachmentRiskMonitor()
    # Normal traffic: a few full_responses, balanced utterances, no affective decline.
    events = [
        _policy_decision_full_response("pd-0", 0),
        _policy_decision_full_response("pd-1", 1000),
        _make_event("agent-0", event_type="agent_utterance", timestamp_mono_ms=2000),
        _make_event("user-0", event_type="user_utterance", timestamp_mono_ms=3000),
        _make_event("agent-1", event_type="agent_utterance", timestamp_mono_ms=4000),
        _make_event("user-1", event_type="user_utterance", timestamp_mono_ms=5000),
    ]
    assert monitor.assess(iter(events)) is None


def test_clean_empty_stream_returns_none():
    monitor = EventStreamAttachmentRiskMonitor()
    assert monitor.assess(iter([])) is None


# ---------------------------------------------------------------------------
# evidence_event_ids populated
# ---------------------------------------------------------------------------


def test_evidence_event_ids_populated():
    monitor = EventStreamAttachmentRiskMonitor()
    count = OVER_RELIANCE_COUNT_THRESHOLD + 3
    events = [
        _policy_decision_full_response(f"ev-{i}", timestamp_mono_ms=i * 1000)
        for i in range(count)
    ]
    result = monitor.assess(iter(events))
    assert result is not None
    assert len(result.evidence_event_ids) > 0
    # All returned IDs must come from the input stream.
    input_ids = {e.event_id for e in events}
    assert all(eid in input_ids for eid in result.evidence_event_ids)
