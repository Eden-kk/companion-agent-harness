"""Stable ReasonCode enum used by every SpeakDecision and DecisionTrace.

See docs/architecture-v0.1.md §Part 5 for the canonical enum members; the
enum is the policy-replay-safe substrate for "why did you say that?".
"""

from enum import Enum


class ReasonCode(Enum):
    EOU_CONFIRMED                = "EOU_CONFIRMED"
    USER_ADDRESSED_AGENT         = "USER_ADDRESSED_AGENT"
    NOT_ADDRESSED_TO_AGENT       = "NOT_ADDRESSED_TO_AGENT"
    BACKCHANNEL_DETECTED         = "BACKCHANNEL_DETECTED"
    ALERT_THRESHOLD_EXCEEDED     = "ALERT_THRESHOLD_EXCEEDED"
    PROACTIVITY_BUDGET_AVAILABLE = "PROACTIVITY_BUDGET_AVAILABLE"
    COOLDOWN_BLOCKED             = "COOLDOWN_BLOCKED"
    QUIET_MODE_BLOCKED           = "QUIET_MODE_BLOCKED"
    PRIVACY_MODE_BLOCKED         = "PRIVACY_MODE_BLOCKED"
    SAFETY_OVERRIDE              = "SAFETY_OVERRIDE"
