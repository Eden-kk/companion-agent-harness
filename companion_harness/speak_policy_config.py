"""Stage 3 speak-policy and EOU config dataclasses (scaffolding only — not wired into SpeakPolicy).

See docs/architecture-v0.1.md §Part 6 Stage 3 for the YAML values these dataclasses encode.

## ADR: why EouPolicyConfig is a separate type from the speak-policy configs

The spec (Part 6 Stage 3) states the "critical separation": EOU thresholds govern whether the
user is done speaking; speak-policy thresholds govern whether the assistant should act.  These
two tuning axes are orthogonal — a crisis mode lowers the alert threshold but must never lower
the EOU threshold (cutting the user off is not a crisis response).  Collapsing them into one
dataclass would make it syntactically possible to couple them during configuration, which the
spec explicitly forbids.  Therefore EouPolicyConfig is a distinct frozen dataclass and the
speak-policy configs (AlertThresholdConfig, AestheticReactionBudgetConfig) are separate types
with no shared base.

These dataclasses are definitions only.  SpeakPolicy.decide() is NOT modified and does NOT
import this module in this scaffolding pass.  Enforcement belongs to the Stage-3 activation
milestone.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AlertThresholdConfig:
    """Per task-mode alert threshold level.

    Encodes speak_policy.alert_threshold from the Part 6 Stage 3 YAML.
    Lower threshold = easier to trigger an alert (safety modes).
    Higher threshold = harder to trigger (focus modes where interruption is costly).

    Spec YAML values:
        cooking:          low      # safety
        crisis_emergency: low      # safety
        creative_focus:   high     # don't interrupt
        normal:           medium
    """

    cooking: str
    crisis_emergency: str
    creative_focus: str
    normal: str


@dataclass(frozen=True)
class AestheticReactionBudgetConfig:
    """Per-mode aesthetic reaction rate limits.

    Encodes speak_policy.aesthetic_reaction_budget from the Part 6 Stage 3 YAML.
    A value of "disabled" means aesthetic reactions are suppressed entirely in that mode.

    Spec YAML values:
        walking_outdoor:   1 per 20 min
        normal:            1 per 60 min
        creative_focus:    disabled
        sleep_winddown:    disabled
        group_unaddressed: disabled
        cooking:           disabled  # safety over aesthetics

    crisis_emergency is not a field here because the spec YAML does not list it.
    speak_policy.decide() treats crisis_emergency as "disabled" (same as cooking)
    — safety over aesthetics, mode not enumerated in this config.
    """

    walking_outdoor: str
    normal: str
    creative_focus: str
    sleep_winddown: str
    group_unaddressed: str
    cooking: str


@dataclass(frozen=True)
class EouPolicyConfig:
    """End-of-utterance detection policy config — kept separate from speak-policy configs.

    Encodes eou_policy from the Part 6 Stage 3 YAML.  See module ADR above for why
    this is a distinct type.

    Spec YAML values:
        user_pause_model:   user-specific (calibrated per-user)
        interruption_cost:  high (never lower casually)
        mode_adjustment:    minimal
    """

    user_pause_model: str
    interruption_cost: str
    mode_adjustment: str


SPEC_ALERT_THRESHOLD = AlertThresholdConfig(
    cooking="low",
    crisis_emergency="low",
    creative_focus="high",
    normal="medium",
)

SPEC_AESTHETIC_REACTION_BUDGET = AestheticReactionBudgetConfig(
    walking_outdoor="1 per 20 min",
    normal="1 per 60 min",
    creative_focus="disabled",
    sleep_winddown="disabled",
    group_unaddressed="disabled",
    cooking="disabled",
)

SPEC_EOU_POLICY = EouPolicyConfig(
    user_pause_model="user-specific",
    interruption_cost="high",
    mode_adjustment="minimal",
)

# Maps AlertThresholdConfig string levels to numeric urgency_score thresholds used
# by the policy layer.  v0.1d initial values — empirical calibration is Stage 3 refinement work.
_LEVEL_TO_FLOAT_THRESHOLD: dict[str, float] = {
    "low": 0.3,
    "medium": 0.6,
    "high": 0.85,
}
