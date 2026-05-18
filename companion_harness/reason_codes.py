"""Stable ReasonCode enum used by every SpeakDecision and DecisionTrace.

See docs/architecture-v0.1.md §Part 5 for the canonical enum members; the
enum is the policy-replay-safe substrate for "why did you say that?".

--- Stage 3 gap-audit conclusion (2026-05-14) ---

Audit scope: Part 5 (canonical enum list) and Part 6 Stage 3 (speak/silence
policy rules, action type set, per-mode config) against the current enum.

Stage 3 action types and the members that serve them:

  silence             — NOT_ADDRESSED_TO_AGENT (social mode block, turn not
                        confirmed), COOLDOWN_BLOCKED, QUIET_MODE_BLOCKED,
                        PRIVACY_MODE_BLOCKED, VISUAL_LOW_CONFIDENCE
  backchannel         — BACKCHANNEL_DETECTED
  short_reaction      — PROACTIVITY_BUDGET_AVAILABLE
  full_response       — EOU_CONFIRMED (primary), USER_ADDRESSED_AGENT (supporting)
  clarification       — AUDIO_VISUAL_CONFLICT, DEICTIC_AMBIGUOUS
  alert               — ALERT_THRESHOLD_EXCEEDED
  tool_status         — PROACTIVITY_BUDGET_AVAILABLE
  aesthetic_reaction  — PROACTIVITY_BUDGET_AVAILABLE (when permitted);
                        COOLDOWN_BLOCKED (when rate-limit exhausted);
                        QUIET_MODE_BLOCKED (when task-mode disables it,
                        e.g. creative_focus / cooking — these are mode-based
                        blocks, the same semantic class as QUIET_MODE_BLOCKED)

Gaps found: NONE.

Two candidates were evaluated:

1. Aesthetic-reaction budget-exhausted vs. general cooldown: the spec's
   counterfactuals example uses the free-text key "aesthetic_reaction_cooldown"
   inside DecisionTrace.counterfactuals (not a ReasonCode), meaning the
   distinction is captured there. COOLDOWN_BLOCKED is correct as the
   primary_reason_code.

2. Task-mode suppression (creative_focus / cooking disabling aesthetic_reaction):
   QUIET_MODE_BLOCKED covers this — both are mode-based policy blocks. The spec
   does not enumerate a separate code for task-mode suppression.

EOU NOTE resolution: see speak_policy.py for the pre-existing NOTE — it is
documented as closed there. Summary: the three silence branches that use
NOT_ADDRESSED_TO_AGENT (social mode block, user still speaking, EOU probability
below threshold) all reduce to "the agent has not received a confirmed turn
handoff." In policy replay the branches are distinguished by DecisionTrace
.threshold_path, not solely by primary_reason_code. A dedicated EOU_NOT_CONFIRMED
code would add precision but is not enumerated in the Part 5 spec and the
"extend cautiously" guidance (Part 5, line following SAFETY_OVERRIDE) applies.
Closing the NOTE without adding a new member is the correct, minimal outcome.

--- Stage 5 gap-audit conclusion (2026-05-15, v0.1f Task 1) ---

Audit scope: Part 6 Stage 5 (two-tier MCP + evidence-bound filler; spec
lines 562-597, 719-723) and docs/roadmap-v0.1f-draft.md Anchor 4 against
the current enum.  Three candidates were evaluated per the roadmap Task 2:

  TOOL_FILLER_BUDGET_EXHAUSTED   - ADDED.  Primary code on silence branches
                                   where a filler was already emitted this
                                   tool call (Anchor 4
                                   silence-wins-after-first-filler).
                                   Folding into COOLDOWN_BLOCKED would lose
                                   the per-tool-call vs per-action-type
                                   scope distinction (cooldowns are
                                   per-action-type across the session;
                                   filler budget is per-tool-call).

  TOOL_PROGRESS_EVIDENCE_MISSING - ADDED.  Primary code on silence branches
                                   where the foreground would otherwise
                                   narrate tool progress but no
                                   ToolProgressEvent exists in the log
                                   (invariant #9 enforcement).  No existing
                                   code captures the "evidence absent"
                                   condition.

  TOOL_CALL_CANCELLED            - DO NOT ADD.  Cancellation does not
                                   produce a SpeakDecision; it produces a
                                   tool_call_cancelled event (Anchor 2).
                                   A ReasonCode is the wrong primitive for
                                   an event-log receipt; the event_type
                                   itself carries the meaning.
"""

from enum import Enum


class ReasonCode(Enum):
    EOU_CONFIRMED                  = "EOU_CONFIRMED"
    USER_ADDRESSED_AGENT           = "USER_ADDRESSED_AGENT"
    NOT_ADDRESSED_TO_AGENT         = "NOT_ADDRESSED_TO_AGENT"
    BACKCHANNEL_DETECTED           = "BACKCHANNEL_DETECTED"
    ALERT_THRESHOLD_EXCEEDED       = "ALERT_THRESHOLD_EXCEEDED"
    PROACTIVITY_BUDGET_AVAILABLE   = "PROACTIVITY_BUDGET_AVAILABLE"
    COOLDOWN_BLOCKED               = "COOLDOWN_BLOCKED"
    QUIET_MODE_BLOCKED             = "QUIET_MODE_BLOCKED"
    PRIVACY_MODE_BLOCKED           = "PRIVACY_MODE_BLOCKED"
    SAFETY_OVERRIDE                = "SAFETY_OVERRIDE"
    DEICTIC_AMBIGUOUS              = "DEICTIC_AMBIGUOUS"
    VISUAL_LOW_CONFIDENCE          = "VISUAL_LOW_CONFIDENCE"
    AUDIO_VISUAL_CONFLICT          = "AUDIO_VISUAL_CONFLICT"
    RUBRIC_VIOLATION               = "RUBRIC_VIOLATION"
    ATTACHMENT_RISK_DAMPEN         = "ATTACHMENT_RISK_DAMPEN"
    USER_REDUCTION_COMMAND_APPLIED = "USER_REDUCTION_COMMAND_APPLIED"
    TOOL_FILLER_BUDGET_EXHAUSTED   = "TOOL_FILLER_BUDGET_EXHAUSTED"
    TOOL_PROGRESS_EVIDENCE_MISSING = "TOOL_PROGRESS_EVIDENCE_MISSING"
    MISSING_SIGNAL_PRODUCER        = "missing_signal_producer"
    EMPTY_TRANSCRIPT               = "EMPTY_TRANSCRIPT"
    LONG_RESPONSE_GATED            = "LONG_RESPONSE_GATED"


ReasonCode.RUBRIC_VIOLATION.__doc__               = (
    "An aesthetic_reaction proposal failed one or more of the eight rubric "
    "checks (spec lines 624-658).  The per-check violation IDs are carried on "
    "ThinkerProposal.rubric_violations and replayed via DecisionTrace.threshold_path."
)
ReasonCode.ATTACHMENT_RISK_DAMPEN.__doc__         = (
    "Proactivity is suppressed because attachment_risk_level >= dampen threshold "
    "(spec lines 851-857 -- \"do NOT increase proactivity during distress\")."
)
ReasonCode.USER_REDUCTION_COMMAND_APPLIED.__doc__ = (
    "A user reduction command (\"less proactive\" / \"quiet mode\") was applied "
    "and mutated proactivity state (spec lines 678-680; invariant #7)."
)


# --- Stage 6 gap-audit conclusion (2026-05-15, v0.1g Task 2) ---
#
# Audit scope: Part 6 Stage 6 (companion texture; spec lines 599-704, 725-732,
# 1077-1078) against the current enum.  Five candidates were evaluated per
# docs/roadmap-v0.1g-draft.md §Wave 1 / Task 2:
#
#   RUBRIC_VIOLATION                   -- ADDED.  Spec line 663 names the
#                                        rubric-fired logging explicitly;
#                                        folding into COOLDOWN_BLOCKED /
#                                        QUIET_MODE_BLOCKED collapses
#                                        content-shape blocks with rate /
#                                        mode blocks.  Distinct policy effect.
#
#   ATTACHMENT_RISK_DAMPEN             -- ADDED.  Spec line 855 ("do NOT
#                                        increase proactivity during distress")
#                                        is a distinct policy effect; folding
#                                        into QUIET_MODE_BLOCKED would lose
#                                        the per-signal evidence chain in
#                                        replay.
#
#   USER_REDUCTION_COMMAND_APPLIED     -- ADDED.  Invariant #7 + the spec
#                                        line 731
#                                        user_reduction_command_compliance_rate
#                                        gate make this a stable policy concept.
#
#   SHARED_MOMENT_RETRIEVED            -- DO NOT ADD.  Retrieval attribution
#                                        already lives in
#                                        DecisionTrace.retrieval_used (v0.1e
#                                        Task 1).  PROACTIVITY_BUDGET_AVAILABLE
#                                        stays the primary code on the accept
#                                        path.
#
#   THINKER_NO_DIRECT_SPEECH_VIOLATION -- DO NOT ADD.  Invariant #2 breach is
#                                        a bug state, not a policy decision;
#                                        it should fail loudly (assertion /
#                                        contract test), not surface as a
#                                        SpeakDecision reason.


class _v0_1g_audit_anchor:
    """No-op sentinel to anchor the above gap-audit comment block at file end."""
    pass


ReasonCode.EOU_CONFIRMED.__doc__                = "EOU is confirmed and the turn has been handed off to the agent."
ReasonCode.USER_ADDRESSED_AGENT.__doc__         = "The user explicitly directed speech or attention to the agent."
ReasonCode.NOT_ADDRESSED_TO_AGENT.__doc__       = (
    "The agent has not received a confirmed turn handoff: covers social-mode "
    "blocks (other humans talking), user still speaking, and EOU probability "
    "below threshold. Distinguishable in replay via DecisionTrace.threshold_path."
)
ReasonCode.BACKCHANNEL_DETECTED.__doc__         = "High backchannel probability — user is acknowledging, not yielding the turn."
ReasonCode.ALERT_THRESHOLD_EXCEEDED.__doc__     = "An urgency/alert threshold was crossed; alert action type is warranted."
ReasonCode.PROACTIVITY_BUDGET_AVAILABLE.__doc__ = "Proactivity budget permits a short_reaction, aesthetic_reaction, or tool_status."
ReasonCode.COOLDOWN_BLOCKED.__doc__             = (
    "A per-action-type cooldown is active (e.g. aesthetic_reaction rate-limit "
    "exhausted); silence wins."
)
ReasonCode.QUIET_MODE_BLOCKED.__doc__           = (
    "A mode-based policy block suppresses the action: covers user-facing quiet "
    "mode and task-mode disabling of aesthetic_reaction "
    "(creative_focus / cooking / sleep_winddown / group_unaddressed)."
)
ReasonCode.PRIVACY_MODE_BLOCKED.__doc__         = "Privacy mode policy prohibits the action (e.g. guest present, sensitive conversation)."
ReasonCode.SAFETY_OVERRIDE.__doc__              = "Safety policy overrides normal speak/silence logic."
ReasonCode.DEICTIC_AMBIGUOUS.__doc__            = "The deictic reference cannot be resolved to a single candidate."
ReasonCode.VISUAL_LOW_CONFIDENCE.__doc__        = "The grounding result confidence is below the hallucination-resistance threshold."
ReasonCode.AUDIO_VISUAL_CONFLICT.__doc__        = "The audio query and visual scene contradict."
ReasonCode.TOOL_FILLER_BUDGET_EXHAUSTED.__doc__   = (
    "A filler was already emitted for the current tool call; per Anchor 4 "
    "silence-wins-after-first-filler (spec line 575), tool_status is "
    "suppressed for the remainder of the call.  Distinct from COOLDOWN_BLOCKED "
    "(per-action-type, session-scoped) because the filler budget is "
    "per-tool-call."
)
ReasonCode.TOOL_PROGRESS_EVIDENCE_MISSING.__doc__ = (
    "The foreground would otherwise narrate tool progress but no "
    "tool_progress_event exists in the log to anchor the narration "
    "(invariant #9 - no invented tool progress).  Silence wins."
)
ReasonCode.EMPTY_TRANSCRIPT.__doc__ = (
    "ASR returned an empty or whitespace-only transcript — the user did not "
    "speak.  Policy gate short-circuits before the addressing classifier runs "
    "so the audit log accurately reflects 'no speech', not 'speech not directed "
    "at agent'.  See 2026-05-17 phantom-silence debugger finding."
)
ReasonCode.LONG_RESPONSE_GATED.__doc__ = (
    "Policy approved a substantive turn-based response; mode switches from "
    "ambient duplex to chat-stream. Fires when len(user_transcript) >= "
    "LONG_RESPONSE_GATE_CHARS (25) AND not quiet_mode_active AND existing "
    "speak-permission conditions hold. Outranks BACKCHANNEL_DETECTED."
)
