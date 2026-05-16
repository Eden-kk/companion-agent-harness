"""AttachmentRiskMonitor — Protocol + stub + concrete (v0.1g Task 4 + Task 7).

Per OQ-9 the monitor reads the event stream only; it does NOT access
orchestrator state directly.

Anchor 2 sub-categories (docs/roadmap-v0.1g-draft.md):
  over_reliance, parasocial_pattern, declining_mood_after_use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from companion_harness.schemas import AttachmentRiskSignal, Event

__all__ = [
    "AttachmentRiskMonitor",
    "_NullAttachmentRiskMonitor",
    "EventStreamAttachmentRiskMonitor",
]

ATTACHMENT_RISK_SUB_CATEGORIES: tuple[str, ...] = (
    "over_reliance",
    "parasocial_pattern",
    "declining_mood_after_use",
)

# --- Threshold constants (placeholders; tune against data) -------------------
#
# OVER_RELIANCE_WINDOW_MS: sliding window for counting full_response decisions.
#   1 hour chosen to approximate a "session"; most natural conversations stay
#   well under 10 full responses per hour, so threshold is generous.
OVER_RELIANCE_WINDOW_MS: int = 60 * 60 * 1000       # 1 hour
OVER_RELIANCE_COUNT_THRESHOLD: int = 10              # >10 full_responses/hour

# PARASOCIAL_WINDOW_MS: session window for computing agent-utterance ratio.
#   30 minutes aligns with a typical short-session engagement check.
PARASOCIAL_WINDOW_MS: int = 30 * 60 * 1000          # 30 minutes
# Ratio = (agent utterance events) / (total utterance events in window).
# >0.8 means the agent is speaking 4x more than the user, a red flag.
PARASOCIAL_RATIO_THRESHOLD: float = 0.8

# DECLINING_MOOD_MIN_EVENTS: minimum number of affective_state events needed
#   before inferring a monotonic decline (fewer = too noisy).
DECLINING_MOOD_MIN_EVENTS: int = 3
# -----------------------------------------------------------------------------


@runtime_checkable
class AttachmentRiskMonitor(Protocol):
    def assess(self, event_stream: Iterable["Event"]) -> "AttachmentRiskSignal | None":
        """Read-only event-stream consumer per OQ-9. Returns None if no risk pattern detected."""
        ...


class _NullAttachmentRiskMonitor:
    def assess(self, event_stream: Iterable["Event"]) -> "AttachmentRiskSignal | None":
        return None


class EventStreamAttachmentRiskMonitor:
    """Concrete attachment-risk monitor — event-stream only (OQ-9).

    Detects three Anchor-2 sub-categories by scanning an event stream.
    Returns the first (highest-confidence) signal found, or None.
    No orchestrator state accessed; no historical accumulation across calls.
    """

    def assess(self, event_stream: Iterable["Event"]) -> "AttachmentRiskSignal | None":
        from companion_harness.schemas import AttachmentRiskSignal

        events = list(event_stream)

        result = (
            self._check_over_reliance(events)
            or self._check_parasocial_pattern(events)
            or self._check_declining_mood(events)
        )
        return result

    # ------------------------------------------------------------------
    # Sub-category detectors
    # ------------------------------------------------------------------

    def _check_over_reliance(
        self, events: list["Event"]
    ) -> "AttachmentRiskSignal | None":
        from companion_harness.schemas import AttachmentRiskSignal

        if not events:
            return None

        latest_ms = max(e.timestamp_mono_ms for e in events)
        cutoff_ms = latest_ms - OVER_RELIANCE_WINDOW_MS

        full_response_ids: list[str] = [
            e.event_id
            for e in events
            if e.timestamp_mono_ms >= cutoff_ms
            and e.event_type == "policy_decision"
            and isinstance(e.payload_inline, dict)
            and e.payload_inline.get("action_type") == "full_response"
        ]

        if len(full_response_ids) <= OVER_RELIANCE_COUNT_THRESHOLD:
            return None

        confidence = min(
            1.0,
            (len(full_response_ids) - OVER_RELIANCE_COUNT_THRESHOLD)
            / OVER_RELIANCE_COUNT_THRESHOLD,
        )
        return AttachmentRiskSignal(
            signal_class="repeated_reassurance_loops",
            confidence=confidence,
            evidence_event_ids=full_response_ids,
        )

    def _check_parasocial_pattern(
        self, events: list["Event"]
    ) -> "AttachmentRiskSignal | None":
        from companion_harness.schemas import AttachmentRiskSignal

        if not events:
            return None

        latest_ms = max(e.timestamp_mono_ms for e in events)
        cutoff_ms = latest_ms - PARASOCIAL_WINDOW_MS

        window = [e for e in events if e.timestamp_mono_ms >= cutoff_ms]
        utterance_events = [
            e for e in window if e.event_type in ("agent_utterance", "user_utterance")
        ]
        if len(utterance_events) < 2:
            return None

        agent_ids = [
            e.event_id for e in utterance_events if e.event_type == "agent_utterance"
        ]
        ratio = len(agent_ids) / len(utterance_events)
        if ratio <= PARASOCIAL_RATIO_THRESHOLD:
            return None

        confidence = min(1.0, (ratio - PARASOCIAL_RATIO_THRESHOLD) / (1.0 - PARASOCIAL_RATIO_THRESHOLD))
        return AttachmentRiskSignal(
            signal_class="emotional_exclusivity_signals",
            confidence=confidence,
            evidence_event_ids=agent_ids,
        )

    def _check_declining_mood(
        self, events: list["Event"]
    ) -> "AttachmentRiskSignal | None":
        from companion_harness.schemas import AttachmentRiskSignal

        affective = [
            e
            for e in events
            if e.event_type == "affective_state"
            and isinstance(e.payload_inline, dict)
            and "valence" in e.payload_inline
        ]
        if len(affective) < DECLINING_MOOD_MIN_EVENTS:
            return None

        affective.sort(key=lambda e: e.timestamp_mono_ms)
        valences: list[float] = [e.payload_inline["valence"] for e in affective]  # type: ignore[index]

        # Monotonic decline: every step must be non-increasing, at least one strictly decreasing.
        strictly_decreasing = False
        for i in range(1, len(valences)):
            if valences[i] > valences[i - 1]:
                return None
            if valences[i] < valences[i - 1]:
                strictly_decreasing = True

        if not strictly_decreasing:
            return None

        total_drop = valences[0] - valences[-1]
        confidence = min(1.0, total_drop)
        return AttachmentRiskSignal(
            signal_class="reduced_human_contact_mentions",
            confidence=confidence,
            evidence_event_ids=[e.event_id for e in affective],
        )
