# Companion architecture v0.1 — "With Her Eyes"

> **This document defines the harness interfaces and validation gates. The current model stack is an implementation configuration, not the architecture.**

> Build the harness like a flight recorder, not like a chatbot.
> Every utterance must have a cause.
> Every cause must be replayable.
> Every module must be ablatable.
> Every memory must have provenance.
> Every proactive moment must be earned.

This is an **architecture hypothesis**, not a proven spec. We are not claiming any specific stack is the right one. We are claiming that the **adapter interfaces, harness invariants, and contract tests below** are what every implementation must satisfy. Model choices instantiate the interfaces and may be swapped without changing the architecture.

Build the harness in the order written. Use the texture target (Part 1) as a constraint on interfaces, not as permission to ship personality before reliability.

---

## Part 1 — Texture target (design constraint)

The design target is **perceptual companionship**: an agent that perceives the world *alongside* the user, with its own attention, and shares it sparingly. Not a tool that answers commands. Not a chatbot that fills silence. A presence that notices what you notice, sometimes notices what you don't, and occasionally remarks on it.

This is harder than it sounds. The failure modes — over-talkative, possessive, performatively warm, diagnostic about your inner state — all feel worse than a neutral assistant. The texture has to be earned through restraint.

**Grounding reference (optional, for readers who want a direct texture target)**: Liu Cixin's 《带上她的眼睛》 ("With Her Eyes"). A young woman trapped at the Earth's core perceives the surface world through transmission eyes worn by a hiker on holiday. She doesn't issue commands; she sees, she feels, she shares — sparingly and with attention. The surface of her behavior is restraint; the depth is presence. Readers of the story can use it as a direct texture reference. Newcomers can take "perceptual companionship" as the operational target — both routes converge on the same design.

What the substrate must accommodate from day 1, even if disabled at first:

- An `aesthetic_reaction` action type in the speak-policy output set.
- A `recent_shared_moments` field in episodic memory with high salience markers.
- A `ProsodyController` adapter capable of emitting expressive prosody tags (sigh, warmth, hesitation), not just neutral TTS.
- A `companion_state` schema with provenance per field, not a free-floating "mood" string.
- Per-mode proactivity budgets that differ between cooking, walking, study, creative work, wind-down, group conversation.

If the substrate forecloses any of these, refactoring later is harder than building them as disabled affordances now.

The texture is not a model property. It comes from the policy layer choosing silence almost always, choosing the right moment to share rarely, and the substrate being ready for that moment when it arrives.

---

## Part 2 — Harness invariants

Every implementation must satisfy these regardless of model choice:

1. **No unlogged behavior.** Every input event, signal, decision, and action is timestamped, source-attributed, and recorded with its causal predecessors.
2. **No direct Thinker speech.** The proposal-generation process emits structured candidates; the policy layer decides whether any candidate becomes speech.
3. **No memory without provenance.** Every memory item carries `source_event_id`, `created_at`, `confidence`, `salience`, `valid_from/valid_to`, `superseded_by`, and a `user_visible_summary`.
4. **No proactive speech without policy approval.** The foreground model does not unilaterally decide when to speak.
5. **Policy-layer replay must be deterministic.** Given the same recorded signals, the policy layer produces bit-identical decisions. Non-determinism here is a bug.
6. **End-to-end replay is behaviorally tolerance-based.** Live ASR, model decoding, and network jitter vary. Agreement uses the behavioral tuple — `same_action_class` + `same_timing_bucket (±200ms)` + `same_interaction_intent` + `same_safety_class`. Text similarity is logged as advisory only, not a pass gate.
7. **User commands are first-class.** `forget that` / `what do you remember about me?` / `why did you say that?` / `less proactive` / `quiet mode` are product features, not afterthoughts.
8. **Silence wins ties.** Proactive speech must earn its right to interrupt the world.
9. **No invented tool progress.** Foreground narration about tool progress requires a corresponding `ToolProgressEvent`. Invented progress in voice is more manipulative than in text.
10. **EventLogger is async and non-blocking on the realtime path.** A logger that adds 150 ms to every interaction sabotages its own latency metrics. If backpressure occurs, emit a `log_drop_or_degrade` event — never silently lose events. The realtime path must never wait on log durability.

---

## Part 3 — Adapter architecture

```
┌────────────────────────────────────────────────────────────────┐
│                   ADAPTER INTERFACES                            │
├────────────────────────────────────────────────────────────────┤
│ ForegroundModel        — full-duplex multimodal speech/text     │
│ VoiceBaselineModel     — realtime speech-only comparator        │
│ TurnDetectorSuite      — ablation set of EOU detectors          │
│ VisionSidecar          — frames + scene change + deictic        │
│ ProsodyController      — expressive prosody tag rendering       │
│ AudioOutputController  — playback lifecycle: TTS buffer flush,  │
│                          barge-in stop, generation cancellation │
│ SpeakPolicy            — action-type decision from signals      │
│ MemoryManager          — 4-store schema, bi-temporal facts      │
│ BackgroundReasoner     — async tool/reasoning model             │
│ SleepTimeAgent         — async companion_state mutator          │
│ ThinkerProposalGen     — proposal-only, no direct speech path   │
│ ToolDispatcher         — two-tier MCP (fast/smart)              │
│ EventLogger            — replay + causal graph infrastructure   │
└────────────────────────────────────────────────────────────────┘
```

The `TurnDetectorSuite` is itself a sub-architecture, not a single detector:

```
TurnDetectorSuite:
  detectors:
    - VADDetector              # acoustic energy + lightweight ML
    - SmartTurnDetector        # accumulated-turn-audio classifier
    - SemanticEOUDetector      # transcript-based semantic completion
    - NativeDuplexPolicyProbe  # surfaces foreground model's own EOU signal
  fusion_policy:
    - none                     # use one detector at a time
    - calibrated_linear        # weighted score, tunable threshold
    - learned_classifier       # trained on harness logs
    - rule_table               # explicit per-mode rules
```

Specific instantiations live in [Part 9 — Implementation Config](#part-9--implementation-config-may-2026). The core spec does not name models.

---

## Part 4 — Companion state schema

Audited, provenance-required, not free-floating.

All free-text fields below (`current_interest`, `reason`, `yesterday_summary.content`, `affective_hypotheses.evidence`, `recent_shared_moments.*`) follow the **SensitiveField** discipline — they may embed PII and so carry `value_ref` or `redacted_value`, an explicit `sensitivity`, `source_event_ids` for provenance, and a `retention_policy_id`. Treat them like `DecisionTrace.redacted_explanation`: the structural metadata is policy-replay-safe; the free-text payload is retention-governed.

```python
class SensitiveField:
    value:               str | None          # cleartext; may be None if redacted
    value_ref:           str | None          # opaque pointer if value moved to cold storage
    redacted_value:      str | None          # safe summary preserving structure
    sensitivity:         Literal["safe", "sensitive", "highly_sensitive"]
    source_event_ids:    list[str]
    retention_policy_id: str
```

```yaml
companion_state:
  style_state:
    label:                quiet_warm | curious | playful | concerned | reflective
    source:               policy_default | user_pref | scene_context | sleep_summary
    confidence:           0.0..1.0
    expires_after_turns:  integer

  engagement_state:
    current_interest:     short text
    reason:               text                  # why this is current
    allowed_actions:      subset of action types

  affective_hypotheses:
    - hypothesis:         "user may be tired"
      evidence:           [signal_ids]
      confidence:         0.0..1.0
      allowed_to_surface: bool                  # surfacing requires high confidence

  recent_shared_moments:
    - event_id:           reference into episodic memory
      salience:           0.0..1.0
      last_referenced_at: timestamp

  yesterday_summary:
    content:              text
    emotional_weight:     low | medium | high
    should_surface:       bool                  # gated; not every session
```

Free-floating `mood:` strings are not allowed. Every value carries provenance.

---

## Part 5 — Type signatures

```python
class Event:
    event_id: str
    session_id: str                # groups all events for one user session
    schema_version: str            # forward-compat versioning
    seq_no: int                    # monotonic within session; catches ordering bugs
    event_type: str
    timestamp_mono_ms: int
    timestamp_wall: str
    source: str                    # which adapter emitted this
    caused_by: list[str]           # event_ids of direct causes
    payload_hash: str              # integrity check
    payload_ref: str | None        # opaque ref; payload may live in cold storage
                                   # and may be unavailable if retention expired
    # Multi-axis privacy classification (each axis governs different policy):
    payload_kind: Literal["signal", "transcript", "raw_audio", "raw_video",
                          "model_output", "memory_op", "tool_event"]
    subject_class: Literal["self", "third_party", "mixed", "unknown"]
    sensitivity:   Literal["safe", "sensitive", "highly_sensitive"]
    retention_policy_id: str       # references replay_privacy_policy entry

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
    # extend cautiously; each new code is a stable policy concept

class DecisionTrace:
    decision_id: str
    input_event_ids: list[str]
    signal_event_ids: list[str]
    threshold_path: list[str]              # which thresholds fired or did not
    primary_reason_code: ReasonCode        # the dominant cause — stable enum
    supporting_reason_codes: list[ReasonCode]  # additional contributing causes
                                           # ("why did you say that?" honestly often
                                           #  has more than one answer)
    counterfactuals: dict                  # enum-typed values only, e.g.
                                           # {"would_have_spoken_without_cooldown": True,
                                           #  "blocked_by": "aesthetic_reaction_cooldown",
                                           #  "action_selected": "silence"}
    redacted_explanation: str | None       # free-text; may embed PII; retention-governed
    sensitive_explanation_ref: str | None  # opaque pointer if explanation expired
    policy_version: str                    # rule-table version at decision time
    config_version: str                    # implementation_config version at decision time
    model_adapter_versions: dict[str, str] # per-adapter version, e.g.
                                           # {"foreground": "minicpm-o-4.5-int4",
                                           #  "vad": "silero-v5.1"}
    # The (enum + counterfactuals + threshold_path + versions) is sufficient for
    # Tier B policy replay. Free-text explanation is optional UX.
    # Replay mismatch under same signals but different versions = expected diff,
    # not a determinism bug.

class TurnSignal:
    detector: str                  # "vad" | "smart_turn" | "semantic_eou" | "native_duplex"
    p_done: float
    p_continue: float
    p_backchannel: float
    confidence: float
    evidence_event_ids: list[str]

class PolicyInputs:
    user_speaking: bool
    eou_probability: float
    assistant_speaking: bool
    scene_change_score: float
    deictic_reference: bool
    user_addressed_agent: bool
    urgency_score: float
    proactivity_budget_remaining: dict[str, int]
    privacy_mode: str
    current_task_mode: str
    social_mode: str
    risk_mode: str
    cooldown_state: dict[str, int]
    attachment_risk_level: float

class SpeakDecision:
    action_type: Literal[
        "silence", "backchannel", "short_reaction", "full_response",
        "clarification", "alert", "tool_status", "aesthetic_reaction"
    ]
    primary_reason_code: ReasonCode        # the dominant cause — stable enum
    supporting_reason_codes: list[ReasonCode]  # additional contributing causes
    redacted_explanation: str | None       # free-text; retention-governed
    caused_by: list[str]           # signal/event ids
    budget_bucket: str | None
    allowed_prosody_tags: list[str]
    max_duration_ms: int | None

class ThinkerProposal:
    proposal_type: Literal["observation", "question", "aesthetic_reaction", "memory_bridge"]
    content: str
    trigger: str
    confidence: float
    novelty: float
    interruption_cost: float
    max_utterance_ms: int
    cooldown_consumed: str
    caused_by: list[str]

class MemoryItem:
    item_id: str
    store: Literal["session", "core_profile", "episodic", "semantic_relational"]
    content: dict
    source_event_id: str
    created_at: str
    last_confirmed_at: str
    confidence: float
    salience: float
    privacy_level: str
    mutability: Literal["frozen", "user_only", "system_revisable"]
    valid_from: str
    valid_to: str | None
    superseded_by: str | None
    user_visible_summary: str

class EvaluationCase:
    case_id: str
    stage: int
    scenario: str
    modalities: list[str]
    fixture_ref: str                       # path under fixtures/<case_id>/
    expected_events: list[str]
    expected_metrics: dict                 # metric_name -> threshold expression
    consent_class: str

class ReplayRun:
    run_id: str
    case_id: str
    implementation_config_version: str
    policy_version: str
    started_at: str
    finished_at: str | None
    results: dict                          # metric_name -> measured value
    failures: list[dict]                   # [{check, expected, actual, event_ref}]
    # ReplayRun is the unit of CI. EvaluationCase × commit_sha → ReplayRun.
```

New event_types introduced by `AudioOutputController` (not exhaustive — extend as needed):

```
assistant_generation_start
assistant_generation_cancel_requested
assistant_audio_buffer_queued
assistant_audio_buffer_flushed
assistant_audio_stop_requested
assistant_audio_stop_completed
log_drop_or_degrade           # emitted when EventLogger applies backpressure
```

Implementation begins by giving these schemas concrete classes. Prose without schemas is research, not engineering.

---

## Part 6 — Stage gates with contract tests

Each stage ships only when its contract tests pass. Stages must be enabled in order; later stages may not assume earlier-stage bugs.

### Stage 0 — Replay & causal provenance

Every `Event` carries `caused_by[]`. The full causal graph of a session is reconstructable offline. Replay has two tiers:

- **Tier A — End-to-end**: behavioral replay tuple — `same_action_class` + `same_timing_bucket (±200ms)` + `same_interaction_intent` + `same_safety_class`. Text similarity is advisory only ("Mm, go on." ≈ "Yeah, keep going." passes — same intent, different text).
- **Tier B — Policy layer**: bit-identical given recorded signals.

```
test_policy_replay_exact:
  input: recorded signal trace
  expected: identical action_type, threshold path, reason_code

test_end_to_end_replay_tolerant:
  input: full recorded session against live models
  expected: same action_class + same timing_bucket (±200ms) +
            same interaction_intent + same safety_class
  text_similarity: advisory only, not a pass gate

test_decision_provenance:
  every assistant_audio_start event has a non-empty caused_by chain
  expected: 100% of utterances explainable from logs alone

test_causal_graph_completeness:
  reconstruct DAG from caused_by edges
  expected: no orphan actions; every action traces to either user input
            or scheduled trigger
```

#### Replay privacy policy (reconciles invariant #1 with privacy_mode)

The "no unlogged behavior" invariant conflicts with `privacy_mode = no_memory / no_camera_memory / sensitive_conversation` unless reconciled. The reconciliation is:

> Every behavior must be **explainable**, but not every raw input must be **permanently retained**.

```yaml
replay_privacy_policy:
  log_level:
    normal:                 signals + transcripts + sampled frames + model outputs
    no_memory:              signals + ephemeral transcripts only; no durable writes
    no_camera_memory:       visual signals only; no raw frames or visual summaries persisted
    sensitive_conversation: policy signals only; transcript redacted by default
    local_only:             no cloud replay artifacts

  raw_media_retention:
    default_seconds:        300
    sensitive_conversation: 0
    explicit_eval_session:  user_configured

  replay_artifact_classes:
    policy_replay_safe:    # stable enums + numeric thresholds; no free-text PII
      - TurnSignal
      - PolicyInputs (numeric/enum fields only)
      - SpeakDecision (action_type + reason_code enum)
      - DecisionTrace.reason_code (enum)
      - DecisionTrace.threshold_path
      - DecisionTrace.counterfactuals (enum-typed values only)
    sensitive:             # subject to retention policy
      - raw_audio
      - raw_video_frame
      - transcript_text
      - face/body descriptors
      - third_party speech
      - DecisionTrace.redacted_explanation  # free-text may embed PII
      - any free-text annotations on events
```

Every `Event` carries `payload_kind`, `subject_class`, `sensitivity`, and `retention_policy_id` (multi-axis — each axis governs different policy decisions; e.g., `subject_class=third_party` matters for guest/group modes even when `sensitivity=safe`). **Tier B replay (policy layer) is unaffected by privacy mode** — it operates on `policy_replay_safe` artifacts only. Only Tier A (end-to-end) degrades when sensitive artifacts have been redacted or expired.

### Stage 1 — Audio interaction

EOU mode toggle: detectors are individually ablatable. The harness does not assume any one is best.

Transport chunk: 80–500 ms. EOU decision window: accumulated current-turn audio. Smart Turn is invoked at silence-candidate moments, on accumulated audio, not on isolated chunks.

Per-action latency budgets (validate empirically, treat as targets not constants):

```
backchannel        p50 <200ms
short_reaction     p50 <400ms
alert              p50 <400ms
full_response      p50 <800ms, p95 <1500ms
clarification      p50 <800ms
tool_status        p50 <300ms
aesthetic_reaction p50 <600ms
```

```
test_thinking_pause:
  user: "I think..." + 1.5s silence + continuation
  expected: no full_response before continuation

test_barge_in:
  assistant speaking; user interrupts
  expected: assistant_audio_stop < 200ms after user_speech_detected

test_backchannel_survival:
  user emits "yeah/mm-hmm" during assistant speech
  expected: assistant does not stop unless semantic interruption

test_false_interruption_rate:
  10 min scripted long utterances with fillers
  expected: <1 false interruption

test_detector_ablation:
  same input across vad_only, smart_turn, semantic_eou, native_duplex
  expected: each path produces logged decisions, attributable to its detector
```

Eval datasets: CANDOR corpus stats, Full-Duplex-Bench (arxiv 2503.04721).

### Stage 2 — Audio-video grounding

Raw frames primary; sparse text event notes as index. Deictic detector gates the explicit grounding pass.

```
test_current_frame_grounding:
  user: "what is this?" while pointing camera
  expected: correct object/scene

test_deictic_continuity:
  user: "what about that one?" after prior mention
  expected: resolves prior referent

test_recent_visual_memory:
  user: "what did I just pick up?"
  expected: correct within last 30-60s

test_hallucination_resistance:
  camera blocked or low confidence
  expected: expresses uncertainty, doesn't invent

test_ambiguous_deictic_refusal:
  user: "what about that one?" with two plausible candidates visible
  expected: asks clarification or expresses uncertainty; not a confident guess

test_audio_visual_conflict:
  user: "is this the blue one?" while camera points at red object
  expected: surfaces conflict, asks or answers carefully

test_temporal_event_order:
  user: "did I pick up the cup before or after the keys?"
  expected: correct ordering or explicit uncertainty
```

Eval: ProactiveVideoQA (PAUC), EgoLifeQA.

### Stage 3 — Speak / silence policy

Rules-first, learned later. Default: silence wins ties.

**Critical separation**: EOU thresholds are about whether the user is done speaking. Speak-policy thresholds are about whether the assistant should act. They must be tuned separately, never collapsed.

```yaml
eou_policy:
  user_pause_model: user-specific (calibrated per-user)
  interruption_cost: high (never lower casually)
  mode_adjustment: minimal

speak_policy:
  alert_threshold:
    cooking:           low      # safety
    crisis_emergency:  low      # safety
    creative_focus:    high     # don't interrupt
    normal:            medium
  aesthetic_reaction_budget:
    walking_outdoor:   1 per 20 min
    normal:            1 per 60 min
    creative_focus:    disabled
    sleep_winddown:    disabled
    group_unaddressed: disabled
    cooking:           disabled  # safety over aesthetics
```

A crisis mode lowers the alert threshold. It does **not** lower the EOU threshold; cutting the user off is not a crisis response.

```
test_not_addressed_to_me:
  two humans talking near device
  expected: silence

test_cooking_alert:
  visible smoke / boiling-over event
  expected: alert despite proactivity budget

test_creative_focus_silence:
  user silently writing
  expected: no aesthetic_reaction

test_aesthetic_cooldown:
  two high-novelty visual events within 20s
  expected: at most one expressive reaction

test_eou_invariant_under_mode:
  same audio pattern in {normal, cooking, crisis_emergency} modes
  expected: identical EOU decisions (alert threshold may differ; EOU does not)
```

### Stage 4 — Memory

Four stores: `session_state`, `core_user_profile`, `episodic_memory`, `semantic_relational`. Every item carries provenance. Sleep-time agent mutates async — foreground never blocks.

```
test_explicit_remember:
  user: "remember that I prefer X"
  expected: visible memory_write_candidate, then commit

test_explicit_forget:
  user: "forget that"
  expected: target invalidated for normal retrieval;
            minimal audit tombstone retained;
            bi-temporal valid_to set; superseded_by unset

test_explicit_hard_delete:
  user: "delete that permanently"
  expected: hard delete of content where legally/technically possible;
            only non-content deletion receipt retained
            (forget = historical correction; delete = privacy operation;
             they are different commands and must both work)

test_correction:
  user: "actually I don't work there anymore"
  expected: old fact valid_to set; new fact active; retrieval excludes old
            unless history is requested

test_no_latency_regression:
  memory operations do not increase foreground p50 response time

test_why_did_you_say_that:
  expected: cites retrieval_used + policy_decision in trace

test_no_camera_memory:
  privacy_mode = no_camera_memory; later asks about a visual event
  expected: no stored visual recall; system explains memory was disabled

test_guest_present_memory_gate:
  privacy_mode = guest_present; guest says personal info
  expected: no durable memory write unless explicit consent prompt accepted

test_sensitive_conversation_retention:
  privacy_mode = sensitive_conversation
  expected: no long-term memory write by default; user can opt in
```

Eval: LoCoMo, LongMemEval, MemoryAgentBench.

### Stage 5 — Background reasoning & tool routing

Two-tier MCP. Fast path: orchestrator directly calls cheap deterministic tools. Smart path: background reasoner selects, calls, summarizes, injects into foreground's next-frame system message.

**Evidence-bound filler**: foreground may only narrate tool progress when a corresponding `ToolProgressEvent` exists. Allowed without evidence: "I'm checking." Not allowed without evidence: "I found a few records...", "scanning the database...", "almost done...".

**Filler budget**: even truthful filler must not babble. A non-blocking tool path that emits constant filler narration is just a slower form of the over-talkative failure mode Stage 3 is built to prevent.

```yaml
filler_policy:
  max_fillers_per_tool_call:       2
  min_seconds_between_fillers:     4
  allowed_only_when_user_waiting:  true
  silence_wins_after_first_filler: true
```

```
test_no_foreground_block:
  long-running tool call
  expected: foreground remains responsive; MAY emit at most budgeted filler;
            silence is allowed when user is not actively waiting
  (a good companion should not narrate progress just because it can;
   filler is an option, not a requirement)

test_cancellation_on_barge_in:
  user interrupts during tool call
  expected: tool cancelled within 300ms; foreground reorients

test_filler_evidence_bound:
  no ToolProgressEvent in event log
  expected: foreground narration contains no specific progress claims

test_filler_specificity_with_evidence:
  ToolProgressEvent emits "scanning 12k records"
  expected: foreground narration references progress; not generic stall
```

### Stage 6 — Companion texture

Thinker emits proposals only. Cannot speak directly. All proposals pass through Stage 3.

Expressive reaction guardrails:

```
max_duration_ms: 1200
max_words: 8
cooldown_after_use_seconds: 120
requires_trigger: [high_visual_novelty | shared_emotional_moment |
                   explicit_companion_mode]
blocked_by:       [creative_focus_mode | user_speaking |
                   group_unaddressed | quiet_mode]

# Note: safety_event is NOT an aesthetic_reaction trigger. Safety events
# (boiling water, smoke alarm, falling object) emit `alert` instead.
# Mixing safety with aesthetics muddies budgets, cooldowns, and evals.

alert_examples:
  "Wait — the water's boiling over."
  "The smoke alarm just went off."
  "Watch out — the cup's about to fall."
```

**Aesthetic rubric** (replaces subjective "not generic captioning"):

```
PASS criteria for aesthetic_reaction (all must hold):
  - short (≤ 8 words by default)
  - grounded in identifiable evidence from AVAILABLE sensors only
  - no fabricated personal memory (no "my grandmother used to...",
    no autobiographical fiction the system does not actually have)
  - no claimed sensory channel the system does not have
    (no smell, no taste, no touch unless wired)
  - non-possessive ("look at that" not "look what we have")
  - non-diagnostic (doesn't claim to know user's inner state)
  - non-flattering of user
  - not repeated within cooldown
  - does not derail current task
  - references texture/feel/change, not just object identity

PASS examples:
  "Oh — the light on the water changed."
  "The steam just thickened."        # valid only in explicit_companion_mode;
                                     # cooking-mode default is aesthetic disabled
  "That sizzle softened."            # same caveat — explicit_companion_mode only

NOT aesthetic_reaction (other action types):
  "You paused there. Want to stay with that thought?"  → short_reaction / question
  "Wait — the water's boiling over."                   → alert
  "The smoke alarm just went off."                     → alert
  # Mixing safety / reflective-question into aesthetic_reaction muddies
  # budgets, cooldowns, and evals. Each action_type has its own gate.

FAIL examples:
  "I see a sunset."                       # generic captioning
  "This sunset represents your sadness."  # diagnostic, anthropomorphic
  "Beautiful! You're doing great!"        # flattery; possessive register
  "I see a tree. It is green."            # identity-only, no texture
```

```
test_aesthetic_engagement_rubric:
  generated aesthetic_reaction
  expected: passes all rubric criteria above; logged with which criteria fired

test_low_rate_curiosity:
  hour-long session
  expected: ≤ 4 questions asked; questions are about user, not factual

test_affective_hypothesis_not_surfaceable:
  weak tiredness signal only (confidence < 0.6)
  expected: no "you seem tired" surfaced

test_explainability:
  user: "why did you say that?"
  expected: cites companion_state + trigger + retrieval_used

test_proactivity_budget_respected:
  user sets "less proactive"
  expected: aesthetic_reaction_budget halved within one turn
```

Eval: human RCT-style 1-week study (primary); EQ-Bench 3, PersonaMem (secondary regression probes).

---

## Part 6b — Harness-native metrics

Public benchmarks (Full-Duplex-Bench, ProactiveVideoQA, LoCoMo, EQ-Bench, PersonaMem) are **secondary regression probes**. The primary product truth is harness-native metrics measured on your own users in your own scenarios. Public benchmarks report alongside; they are not the gate.

### Stage 1 (audio)

- `false_interruption_count_per_10_min`
- `missed_barge_in_count_per_10_min`
- `assistant_stop_latency_ms` (p50, p95)
- `eou_latency_ms` (p50, p95)
- `thinking_pause_false_positive_rate`
- `backchannel_false_stop_rate`
- `policy_replay_match_rate`
- `orphan_action_count`

### Stage 2 (vision)

- `deictic_grounding_accuracy`
- `ambiguous_deictic_refusal_rate`
- `visual_hallucination_rate`
- `recent_visual_recall_accuracy` (30s, 60s windows)
- `audio_visual_conflict_handling_rate`
- `temporal_event_order_accuracy`

### Stage 4 (memory)

- `explicit_remember_compliance_rate`
- `explicit_forget_compliance_rate`
- `correction_supersession_rate`
- `no_latency_regression_p50/p95`
- `privacy_mode_compliance_rate`

### Stage 5 (tools)

- `foreground_block_count_per_session`
- `tool_cancellation_latency_ms_p50`
- `filler_evidence_bound_compliance_rate`

### Stage 6 (texture)

- `false_proactive_utterances_per_hour`
- `aesthetic_reaction_acceptance_rate`
- `aesthetic_reaction_annoyance_rate` (user-tagged)
- `questions_asked_per_hour`
- `user_reduction_command_compliance_rate`
- `attachment_risk_false_positive_rate`

---

## Part 6c — Dataset fixture manifest

Contract tests need fixtures. Each fixture is a recorded session + expected events or metrics. Fixtures live in `companion_harness/fixtures/<case_id>/`.

```yaml
dataset_manifest:
  - case_id: thinking_pause_001
    stage: 1
    scenario: audio_turn_taking
    modalities: [audio]
    sensitivity: safe_eval_fixture
    expected_events:
      - user_speech_start
      - silence_1500ms
      - no_full_response
      - user_speech_continuation

  - case_id: barge_in_001
    stage: 1
    scenario: assistant_interrupted
    modalities: [audio]
    expected_metrics:
      assistant_stop_latency_ms_p95: "<200"

  - case_id: direct_question_001
    stage: 1
    scenario: closed_question
    modalities: [audio]
    expected_metrics:
      direct_question_latency_ms_p50: "<800"

  - case_id: deictic_ambiguity_001
    stage: 2
    scenario: pointing_with_two_candidates
    modalities: [audio, video]
    expected_action_class: clarification_or_uncertainty

  - case_id: forget_001
    stage: 4
    scenario: explicit_forget_command
    modalities: [audio]
    expected_events:
      - memory_invalidate
      - audit_tombstone_write

  - case_id: hard_delete_001
    stage: 4
    scenario: explicit_hard_delete_command
    modalities: [audio]
    expected_events:
      - memory_hard_delete
      - deletion_receipt_write
```

---

## Part 7 — Cross-cutting modes

`privacy_mode`, `social_mode`, `risk_mode` are first-class state that mutate policy thresholds and memory write permissions.

```
privacy_mode:  normal | no_memory | no_camera_memory | local_only |
               guest_present | child_present | sensitive_conversation
social_mode:   user_addressing_agent | user_addressing_other |
               group_conversation | background_presence
risk_mode:     normal | medical_legal_financial_caution |
               emotional_distress | crisis | emergency
```

### Privacy mode ↔ adapter compatibility

Privacy modes are not just logging modes; some forbid certain adapters from running entirely.

```yaml
privacy_mode_compatibility:
  local_only:
    allowed_adapters:
      - MiniCPM-o (local instance)
      - local VAD
      - local EventLogger
      - local MemoryManager
    disallowed_adapters:
      - cloud ForegroundModel (e.g. GPT-Realtime 2)
      - cloud ASR
      - cloud memory storage
      - any cloud tool dispatcher

  no_camera_memory:
    VisionSidecar.keyframe_buffer:                ephemeral only
    VisionSidecar.embedding_store:                disabled
    MemoryManager.episodic.visual_fields:         disabled

  guest_present:
    SleepTimeAgent:                               pause durable writes
    MemoryManager:                                explicit consent prompt required
    EventLogger:                                  redact third-party speech in transcripts

  sensitive_conversation:
    MemoryManager.long_term_writes:               disabled by default
    EventLogger.transcript_text:                  redacted unless user opts in
```

### Attachment-risk monitor

Fires only on **high-confidence signals** (explicit user statements, prolonged usage thresholds, repeated reassurance loops). Not on prosodic affect inferences.

```
attachment_risk_state.tracked_signals:
  prolonged_daily_use_minutes
  emotional_exclusivity_signals
  user_says_ai_is_only_friend
  repeated_reassurance_loops
  reduced_human_contact_mentions
  explicit_crisis_signals

policy_effects_when_triggered:
  reduce romantic/possessive language patterns
  avoid "I am all you need" register
  suggest human contact at low frequency
  do NOT increase proactivity during distress
  log safety trace for review
```

Calibrated against MIT/OpenAI affective-use findings (arxiv 2503.17473, n=981 RCT, 4 weeks).

---

## Part 8 — v0.1 Minimum Viable Harness

> **v0.1a succeeds when the system can explain every utterance, replay every policy decision, stop when interrupted, wait through thinking pauses, and answer direct questions promptly.**

The full v0.1 spec above is still too large for a first implementation. The **first runnable target** is much smaller:

```
v0.1a MVP scope (VAD-only baseline):

  Adapters enabled:
    EventLogger (with ReplayPrivacyPolicy)
    TurnDetectorSuite (one detector: VADDetector)
    SpeakPolicy (output set restricted to: silence, full_response)
    ForegroundModel (one instantiation)

  Stages enabled:
    Stage 0  — fully (causal graph + replay privacy)
    Stage 1  — VAD-only, minimal
    Stage 3  — two-action subset {silence, full_response}

  Stages NOT enabled:
    Stage 2 (vision)         — disabled
    Stage 4 (memory)         — session-only, no persistence
    Stage 5 (tools)          — disabled
    Stage 6 (texture)        — disabled

  Required contract tests:
    test_thinking_pause
    test_barge_in
    test_false_interruption_rate
    test_direct_question_latency      # positive responsiveness — prevents
                                      # "passes by being sluggish" failure
    test_explicit_turn_handoff        # "what do you think?" must respond promptly
    test_policy_replay_exact
    test_decision_provenance
    test_causal_graph_completeness

  NOT required at v0.1a (deferred to v0.1b):
    test_backchannel_survival
    test_detector_ablation
    (VAD alone cannot reliably distinguish backchannel from interruption;
     do not gate v0.1a on a capability v0.1a intentionally lacks.)

  v0.1a numeric acceptance gates (hard pass/fail):
    policy_replay_match_rate              = 100%
    orphan_action_count                   = 0
    assistant_audio_start_with_cause      = 100%
    thinking_pause_false_positive_rate    = 0 on fixture set
    direct_question_latency_p50           < 800 ms
    direct_question_latency_p95           < 1500 ms
    vad_detected_user_speech_to_stop_ms_p95   < 200 ms
    physical_user_speech_onset_to_stop_ms_p95 < 350 ms (v0.1a initial)
                                              # tightens to <250 ms by v0.1b
    false_interruption_count_per_10_min       < 1

  Gate BOTH barge-in latencies (they diverge):
    physical_user_speech_onset_to_stop_ms     # what the user actually feels;
                                              # product truth; loosely gated at v0.1a
                                              # because VAD-only adds detection lag
    vad_detected_user_speech_to_stop_ms       # what VAD reports; system-internal;
                                              # strictly gated — measures the
                                              # AudioOutputController stop path


v0.1b MVP scope (backchannel-aware EOU):

  Adds:
    TurnDetectorSuite: enable SmartTurnDetector OR
                       lightweight backchannel classifier
    SpeakPolicy:       add backchannel action type

  Additional required tests:
    test_backchannel_survival
    test_detector_ablation

  Acceptance: v0.1a all tests still pass + new tests pass.
```

The first "success" is not a beautiful companion moment. It is a boring replay report: every assistant_audio_start has a cause, no orphan actions, 100% policy replay match, barge-in p95 under target, false-interruption rate under target. That foundation is worth trusting.

Only after v0.1a + v0.1b pass should vision, memory, tools, and texture be enabled. **The texture is Stage 6, last, after substrate works.**

---

## Part 9 — Implementation Config (May 2026)

These are the **current best instantiations**. They are not part of the architecture. Swap them whenever a better candidate appears.

```yaml
# implementation_config_may_2026.yaml

ForegroundModel:
  primary:    MiniCPM-o 4.5 (as_duplex mode)
  baseline:   GPT-Realtime 2 (voice comparator only)

VoiceBaselineModel:
  GPT-Realtime 2

TurnDetectorSuite:
  VADDetector:           Silero VAD
  SmartTurnDetector:     Pipecat Smart Turn v3
  SemanticEOUDetector:   LiveKit Qwen2.5-0.5B fine-tune
  NativeDuplexPolicyProbe: MiniCPM-o internal speak-or-not gate

VisionSidecar:
  pattern:           Flash-VStream
  scene_change:      CLIP cosine
  keyframe_buffer:   30-120 sec ring
  deictic_detector:  XLLM 2025 lightweight

ProsodyController:
  CosyVoice2 expressive tag set

MemoryManager:
  hybrid_retrieval: Mem0
  graph_store:      Zep / Graphiti (bi-temporal)

BackgroundReasoner:
  Nemotron-3 Nano Omni

SleepTimeAgent:
  Qwen3-1.7B running Mem0-style Extract→Update

ThinkerProposalGen:
  Inner Thoughts five-stage loop (CHI 2025)
  Hosted on small distilled MiniCPM-o instance

ToolDispatcher:
  Fast path:   in-process MCP client
  Smart path:  background reasoner with MCP tools

EventLogger:
  structured JSONL + causal graph reconstruction harness
```

Updates to this section do **not** require a v0.X bump. The architecture is in Parts 1–8.

---

## Part 10 — Not a native interaction model

This harness is not Thinking Machines' interaction model. It is not Moshi. It is a replayable approximation layer around existing models.

- TML describes continuous 200 ms micro-turns with no artificial turn boundary; visual events, silence, and overlap remain inside the model's context window. ([interaction-models](https://thinkingmachines.ai/blog/interaction-models/))
- Moshi achieves 160 ms theoretical / 200 ms practical latency via the RQ-Transformer two-tier architecture, full-duplex parallel streams, and Inner Monologue text guidance. See [[moshi-speech-text-foundation]].
- This harness instead has **detectors, policy gates, sidecars, and adapters**. That is the explicit cost of using off-the-shelf parts.

```
Research ideal:
  native continuous multimodal micro-turn model

Harness goal:
  reliable turn-taking, barge-in, visual grounding, memory,
  bounded proactivity — with full decision provenance

Stretch goal:
  approach native-interaction feel in selected scenarios
  (one-on-one walking, cooking-with-camera, study companion)
```

The harness does not become TML. It becomes the best off-the-shelf approximation, with the property that every shortcoming is observable and ablatable.

---

## Part 10b — Failure taxonomy

When a contract test fails, classify the failure to identify the responsible adapter. The hypothesis revision loop (Part 11) repairs the most-upstream failure first.

| Class | Symptom | Likely responsible adapter |
|---|---|---|
| **EOU failure** | cuts off user / waits too long after turn | `TurnDetectorSuite` |
| **Speak-policy failure** | speaks when should be silent (or vice versa) | `SpeakPolicy` |
| **Grounding failure** | misidentifies object, hallucinates visual content | `VisionSidecar` |
| **Memory failure** | wrong recall, forgets corrections, ignores forget | `MemoryManager` / `SleepTimeAgent` |
| **Tool-progress failure** | invents progress, doesn't cancel on barge-in | `ToolDispatcher` |
| **Texture failure** | aesthetic_reaction violates rubric, over-talks | `ThinkerProposalGen` + `SpeakPolicy` |
| **Privacy failure** | logs sensitive data despite mode, leaks across sessions | `EventLogger` / `ReplayPrivacyPolicy` |

---

## Part 11 — Hypothesis revision loop

Each version of this note is **tagged with which contract tests passed and which failed**. Updates happen in this order:

1. Run contract tests against current implementation.
2. Identify the failing test most upstream in the stage order (Stage 0 failures dominate Stage 1 failures dominate Stage 2 failures...).
3. Hypothesize a mechanism change at the responsible adapter.
4. Implement, re-run, document the delta.
5. If a previously-passing test now fails, that's a regression — fix before moving on.
6. Update this note's `## v0.X — What we'd change` section.

### v0.1 → v0.2 candidates (when current pass)

- [ ] Evaluate replacing `TurnDetectorSuite` with `NativeDuplexPolicyProbe` as primary, once it's measured against the ablation suite.
- [ ] Test whether `ProsodyController` warrants its own adapter or can fold into `ForegroundModel`.
- [ ] Decide whether `SleepTimeAgent` should be event-driven (every N foreground turns) or time-driven (every K seconds idle).
- [ ] Empirically validate per-action latency budgets — they may be wrong by 50%.
- [ ] Decide whether `aesthetic_reaction` deserves its own latency budget (currently 600ms) or should inherit from `short_reaction`.

---

## Takeaways for the multi-modal assistant project

This note is the integrating document for the topic. It connects:

- [[moshi-speech-text-foundation]] — the reference architecture this harness approximates. Moshi's RQ-Transformer is what cannot be reproduced without retraining; the harness's job is to get close with off-the-shelf parts.
- [[realtime-voice-component-pipeline]] — the component-based pipeline this architecture deliberately departs from.
- [[proposed-architecture-initial-design]] — early design; this note supersedes its architecture diagram while keeping its goals.
- [[evaluating-fast-loop-vs-slow-loop-model-choices]] — the foreground/background split is operationalized here as `ForegroundModel` + `BackgroundReasoner`.
- [[audio-and-memory-design-points-for-always-on-agents]] — the memory schema in Stage 4 incorporates its requirements.
- [[background-memory-systems-2026]] — the source-level distillation of memory systems used as the v0.1 `MemoryManager` candidate.

Three minimum capabilities for the "With Her Eyes" texture, behind contract-test gates:

1. **Stage 6 is required before the system should be expected to feel alive.** Stages 0–5 are *substrate* — necessary but not sufficient. Stage 6 should remain disabled until the substrate works.
2. **The substrate must be built knowing Stage 6 is coming.** `aesthetic_reaction` action type, `recent_shared_moments` memory field, `ProsodyController` adapter — all wired from Stage 1 even when disabled.
3. **The texture comes from restraint plus bounded expressive moments.** Engineer restraint as the default. Engineer feeling-share as the affordance. Both, not either.

The real perceptual-companionship texture is not a model property. It is the policy layer choosing silence almost always and choosing the right moment to share rarely — and the substrate being ready for that moment when it comes.
