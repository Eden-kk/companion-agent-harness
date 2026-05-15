"""AttachmentRiskMonitor — Protocol + stub (v0.1g Task 4).

Per OQ-9 the monitor reads the event stream only; it does NOT access
orchestrator state directly. The concrete monitor is v0.1g Task 7.

Anchor 2 sub-categories (docs/roadmap-v0.1g-draft.md):
  over_reliance, parasocial_pattern, declining_mood_after_use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

if TYPE_CHECKING:
    from companion_harness.schemas import AttachmentRiskSignal, Event

__all__ = ["AttachmentRiskMonitor", "_NullAttachmentRiskMonitor"]

ATTACHMENT_RISK_SUB_CATEGORIES: tuple[str, ...] = (
    "over_reliance",
    "parasocial_pattern",
    "declining_mood_after_use",
)


@runtime_checkable
class AttachmentRiskMonitor(Protocol):
    def assess(self, event_stream: Iterable["Event"]) -> "AttachmentRiskSignal | None":
        """Read-only event-stream consumer per OQ-9. Returns None if no risk pattern detected."""
        ...


class _NullAttachmentRiskMonitor:
    def assess(self, event_stream: Iterable["Event"]) -> "AttachmentRiskSignal | None":
        return None  # UNAVAILABLE: concrete monitor is v0.1g Task 7
