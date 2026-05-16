"""tests for EventStreamAttachmentRiskMonitor wiring into live PolicyInputs (closes #213).

Success criterion: pytest tests/test_attachment_risk_level_live_wired.py -v → all pass.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from companion_harness.attachment_risk_monitor import (
    OVER_RELIANCE_COUNT_THRESHOLD,
    EventStreamAttachmentRiskMonitor,
)
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, PolicyInputs, TurnSignal
from manual_test_console.live_pipeline import (
    _make_live_policy_inputs_builder,
    build_live_pipeline,
    EnergyVADModel,
)


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


def _make_turn_signal() -> TurnSignal:
    return TurnSignal(
        detector="test",
        p_done=0.9,
        p_continue=0.1,
        p_backchannel=0.0,
        confidence=1.0,
        evidence_event_ids=["ev-0"],
    )


def _high_risk_events(extra: int = 2) -> list[Event]:
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


# ---------------------------------------------------------------------------
# test_attachment_risk_level_sourced_from_monitor
# ---------------------------------------------------------------------------


def test_attachment_risk_level_sourced_from_monitor():
    """Builder uses monitor.current_level() when monitor is provided."""
    monitor = EventStreamAttachmentRiskMonitor()
    builder = _make_live_policy_inputs_builder(attachment_risk_monitor=monitor)

    signal = _make_turn_signal()
    inputs = builder(signal, [signal])
    # Monitor has no events yet → level = 0.0
    assert inputs.attachment_risk_level == 0.0

    # Pump high-risk events into monitor synchronously
    asyncio.run(_pump_events(monitor, _high_risk_events()))

    inputs2 = builder(signal, [signal])
    assert inputs2.attachment_risk_level > 0.0


async def _pump_events(monitor: EventStreamAttachmentRiskMonitor, events: list[Event]) -> None:
    for evt in events:
        await monitor.on_event(evt)


# ---------------------------------------------------------------------------
# test_default_attachment_risk_when_monitor_quiet
# ---------------------------------------------------------------------------


def test_default_attachment_risk_when_monitor_quiet():
    """When monitor has seen no risk events, current_level() returns 0.0."""
    monitor = EventStreamAttachmentRiskMonitor()
    builder = _make_live_policy_inputs_builder(attachment_risk_monitor=monitor)
    signal = _make_turn_signal()
    inputs = builder(signal, [signal])
    assert inputs.attachment_risk_level == 0.0


# ---------------------------------------------------------------------------
# test_attachment_risk_dampen_gate_fires_in_live
# ---------------------------------------------------------------------------


def test_attachment_risk_dampen_gate_fires_in_live():
    """End-to-end: high-risk events → monitor.current_level() >= 0.5 → builder produces
    attachment_risk_level >= 0.5 (speak_policy will silence proactive speech)."""
    from companion_harness.speak_policy import decide

    monitor = EventStreamAttachmentRiskMonitor()
    builder = _make_live_policy_inputs_builder(attachment_risk_monitor=monitor)

    # extra=OVER_RELIANCE_COUNT_THRESHOLD → confidence = 1.0
    asyncio.run(_pump_events(monitor, _high_risk_events(extra=OVER_RELIANCE_COUNT_THRESHOLD)))
    assert monitor.current_level() >= 0.5

    signal = _make_turn_signal()
    inputs = builder(signal, [signal])
    assert inputs.attachment_risk_level >= 0.5

    # Now verify the dampen gate fires in speak_policy for a proactive attempt
    proactive_inputs = PolicyInputs(
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
        attachment_risk_level=inputs.attachment_risk_level,
        aesthetic_novelty_score=0.8,
    )
    decision = decide(proactive_inputs, ["sig-1"])
    assert decision.action_type == "silence"
    assert decision.primary_reason_code == ReasonCode.ATTACHMENT_RISK_DAMPEN


# ---------------------------------------------------------------------------
# test_no_unavailable_213_marker_remains
# ---------------------------------------------------------------------------


def test_no_unavailable_213_marker_remains():
    """Verify the UNAVAILABLE marker for issue 213 no longer exists in source."""
    import re
    from pathlib import Path

    # Build pattern without writing the literal marker text here (avoids false positives
    # in test_unavailable_markers_have_issues scanner).
    _issue = "213"
    marker_re = re.compile(r"#\s*UNAVAILABLE:\s*#" + _issue + r"\b")
    this_file = Path(__file__).resolve()
    repo_root = Path(__file__).parent.parent
    hits: list[str] = []
    for root in ("companion_harness", "manual_test_console", "tests"):
        base = repo_root / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if path.resolve() == this_file:
                continue
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if marker_re.search(line):
                    hits.append(f"{path}:{lineno}")
    assert not hits, f"Stale markers for issue {_issue} remain: {hits}"
