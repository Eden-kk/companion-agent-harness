"""companion_state — Part 4 audited state record (Stage 4 scaffolding, schema only).

See docs/architecture-v0.1.md §Part 4 for the full schema.  This module
defines frozen dataclasses only — no state machine, no mutation logic.

Free-text fields (`current_interest`, `reason`, `yesterday_summary.content`,
`affective_hypotheses.hypothesis`, `affective_hypotheses.evidence`) carry
SensitiveField per CLAUDE.md ("Free-text fields go through SensitiveField")
and Part 4's SensitiveField discipline note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from companion_harness.schemas import SensitiveField

__all__ = [
    "StyleState",
    "EngagementState",
    "AffectiveHypothesis",
    "RecentSharedMoment",
    "YesterdaySummary",
    "CompanionState",
]


@dataclass(frozen=True)
class StyleState:
    label:               Literal["quiet_warm", "curious", "playful", "concerned", "reflective"]
    source:              Literal["policy_default", "user_pref", "scene_context", "sleep_summary"]
    confidence:          float
    expires_after_turns: int


@dataclass(frozen=True)
class EngagementState:
    current_interest: SensitiveField
    reason:           SensitiveField
    allowed_actions:  tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.current_interest, SensitiveField):
            raise TypeError(f"current_interest must be SensitiveField, got {type(self.current_interest)!r}")
        if not isinstance(self.reason, SensitiveField):
            raise TypeError(f"reason must be SensitiveField, got {type(self.reason)!r}")


@dataclass(frozen=True)
class AffectiveHypothesis:
    hypothesis:         SensitiveField
    evidence:           SensitiveField
    confidence:         float
    allowed_to_surface: bool

    def __post_init__(self) -> None:
        if not isinstance(self.hypothesis, SensitiveField):
            raise TypeError(f"hypothesis must be SensitiveField, got {type(self.hypothesis)!r}")
        if not isinstance(self.evidence, SensitiveField):
            raise TypeError(f"evidence must be SensitiveField, got {type(self.evidence)!r}")


@dataclass(frozen=True)
class RecentSharedMoment:
    event_id:           str
    salience:           float
    last_referenced_at: str


@dataclass(frozen=True)
class YesterdaySummary:
    content:          SensitiveField
    emotional_weight: Literal["low", "medium", "high"]
    should_surface:   bool

    def __post_init__(self) -> None:
        if not isinstance(self.content, SensitiveField):
            raise TypeError(f"content must be SensitiveField, got {type(self.content)!r}")


@dataclass(frozen=True)
class CompanionState:
    style_state:           StyleState
    engagement_state:      EngagementState
    affective_hypotheses:  tuple[AffectiveHypothesis, ...]
    recent_shared_moments: tuple[RecentSharedMoment, ...]
    yesterday_summary:     YesterdaySummary
