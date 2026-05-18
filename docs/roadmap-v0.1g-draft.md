# Roadmap — v0.1g (DRAFT)

## Status: **DRAFT** — converged 2026-05-15 (parallel with v0.1f; recovered from shared-worktree race).

> v0.1g scope is **Stage 6 — Companion texture** from
> `docs/architecture-v0.1.md` Part 6. Drafted in parallel with v0.1f
> (Stage 5); Stage 6 has hard dependencies on the
> `proactivity_budget_remaining` alphabet locked by v0.1f and on the
> POLICY_VERSION bump that v0.1f executes.
>
> The defining design discipline of this milestone is **invariant #2**:
> ThinkerProposalGen emits proposals only — never speech — and every
> proposal goes through `SpeakPolicy.decide()` (invariant #4). Stage 6
> is the milestone where the system can sound like a companion without
> *becoming* one in a way the user did not ask for. The eight-check
> aesthetic-reaction rubric is the rubric that keeps it on the right
> side of that line.

## v0.1g pinned success criterion

> **v0.1g succeeds when ThinkerProposalGen emits proposals only
> (invariant #2); all proposals pass through `SpeakPolicy.decide()`
> (invariant #4); `aesthetic_reaction` content respects all eight rubric
> checks (short ≤8 words, grounded, non-possessive, non-diagnostic,
> non-flattering, no fabricated memory, no absent sensory channel, not
> identity-only); per-mode proactivity budgets are enforced; the
> attachment-risk monitor flags concerning patterns with false-positive
> rate gated; the 1-week RCT methodology is defined as primary product
> eval (advisory; not a release gate); and all earlier-stage gates
> remain green. Four harness-derived metrics also gate:
> `rubric_compliance_rate`, `aesthetic_reaction_cooldown_compliance_rate`,
> `recent_shared_moments_attribution_rate`,
> `thinker_direct_speech_violation_count`.**

## Anchor decisions — non-transient (locked)

### Anchor 1 — Rubric schema on `ThinkerProposal`

New field on `ThinkerProposal`:
`rubric_violations: list[RubricViolation]` (default `[]`).
`RubricViolation` enum locks 8 IDs:

- `RUBRIC_TOO_LONG`
- `RUBRIC_UNGROUNDED`
- `RUBRIC_POSSESSIVE`
- `RUBRIC_DIAGNOSTIC`
- `RUBRIC_FLATTERING`
- `RUBRIC_FABRICATED_MEMORY`
- `RUBRIC_ABSENT_SENSORY_CHANNEL`
- `RUBRIC_IDENTITY_ONLY`

Spec lines 624–658 enumerate 10 PASS criteria; Anchor-1 enum locks the
8 that map to discrete violation IDs (cooldown and task-derail are
already covered by existing reason codes).

### Anchor 2 — Attachment-risk signal/event shape

New `AttachmentRiskSignal` payload + new `attachment_risk_signal`
event_type. `TrackedSignal` payload type carries:

- sub-category (over-reliance, parasocial pattern, declining-mood-after-use)
- `evidence_event_ids`
- `confidence`

### Anchor 3 — `recent_shared_moments` source-of-truth

**Projection over `episodic_memory`** (OQ-7 locked). Computed
**on-demand** by `EpisodicMemoryStore.retrieve_shared_moments(n=5)` at
every `aesthetic_reaction` decision. `CompanionState.recent_shared_moments`
is a read-through accessor backed by the projection call, NOT a
sleep-time-agent-cached snapshot. Preserves invariant #5 trivially
(no extra state to keep in sync).

### Anchor 4 — Rubric enforcement position: inside `SpeakPolicy.decide()`

Per OQ-1: keeps rubric-blocked proposals visible (invariant #1);
rubric-pass becomes part of `threshold_path` → bit-identical replay
(invariant #5). NOT pre-filtered at proposal-gen.

## Closed decisions (OQs 1–10, all RESOLVED 2026-05-15)

- **OQ-1**: Rubric enforcement inside `SpeakPolicy.decide()`.
- **OQ-2**: Rubric checker = regex/lexicon at v0.1g (no learned classifier).
- **OQ-3**: `false_proactive_utterances_per_hour` = user-rejected (a)
  for harness gate, human-rated (b) for RCT.
- **OQ-4**: `ProsodyController` is `TtsAdapter` per `tts_adapter.py:1–8`
  (mapping already in code). Stage 6 doesn't expand it per OQ-10.
- **OQ-5**: 1-week RCT is NOT a v0.1g harness gate.
- **OQ-6**: Hard dependency on v0.1f naming the
  `proactivity_budget_remaining` alphabet. v0.1f locked it as
  empty-set; the fallback path (halve only `aesthetic_reaction`)
  becomes unnecessary.
- **OQ-7**: `recent_shared_moments` = projection over `episodic_memory`.
- **OQ-8**: `attachment_risk_level` = current-snapshot only.
- **OQ-9**: `AttachmentRiskMonitor` reads event-stream only.
- **OQ-10**: Stage 6 does NOT change `TtsAdapter`.

## Tasks (21 tasks across 8 waves)

### Wave 1 — Schema foundation

- **Task 1 + 2 (atomic merge).** Schema foundation: `ThinkerProposal`
  with `rubric_violations`; `RubricViolation` enum; `AttachmentRiskSignal`
  payload + event type. **(Already shipped: PR #129.)**

### Wave 2 — Protocols + stubs

- **Task 3.** `AestheticRubric` Protocol + stub.
- **Task 4.** `AttachmentRiskMonitor` Protocol + stub.

### Wave 3 — Concrete rubric/monitor + policy hook

- **Task 5.** SpeakPolicy hook:
  `if proposal.rubric_violations: → RUBRIC_VIOLATION ReasonCode`.
  POLICY_VERSION conditional bump TBD by v0.1f cooperation
  (see §10 coordination).
- **Task 6.** Concrete rubric (regex/lexicon, 8 enum IDs).
- **Task 7.** Concrete attachment-risk monitor (event-stream-only,
  per OQ-9).

### Wave 4 — `recent_shared_moments` wiring

- **Task 8.** `recent_shared_moments` wiring.
  **Touched files:**
  - `memory_manager.py` (Protocol extension)
  - `episodic_memory_store.py` (concrete `retrieve_shared_moments`)
  - `session_state_store.py` / `core_user_profile_store.py` /
    `semantic_relational_store.py` (pass-through no-op for Protocol
    compliance)
  - test doubles in `tests/test_*.py`

  **Success criterion:** "no `isinstance(obj, MemoryManager)` callsite
  returns `False`."

### Wave 5 — User reduction commands

- **Task 9.** `user_reduction_command_applied` — "less proactive" /
  "quiet mode" semantics. Coordination with v0.1f budget-key alphabet
  (resolved: empty set; halve `aesthetic_reaction`).

### Wave 6 — Contract tests (9 tests)

- **Task 10.** `test_aesthetic_engagement_rubric` (8 `RubricViolation` IDs).
- **Task 11.** `test_aesthetic_grounded` (no fabricated memory /
  absent sensory).
- **Task 12.** `test_affective_hypothesis_not_surfaceable` (no
  diagnostic).
- **Task 13.** `test_no_possessive_or_flattering`.
- **Task 14.** `test_attachment_risk_dampen` (true positive).
- **Task 15.** `test_proactivity_budget_respected` (Stage 3 carryforward
  + new keys).
- **Task 16.** `test_shared_moment_referenced` (`recent_shared_moments`
  retrieval).
- **Task 17.** `test_aesthetic_cooldown` (Stage 3 carryforward, Stage 6
  enabled).
- **Task 18.** `test_thinker_no_direct_speech` (asserts no
  `assistant_audio_buffer_queued` traces back to anything OTHER than
  the v0.1e[^1] policy-decision event; `orphan_count == 0`).

### Wave 7 — Replay extension + report + tag

- **Task 19.** `test_policy_replay_exact` extension with 2 new
  `threshold_path` strings + `AttachmentRiskMonitor.update()`
  pure-function check (identical event stream → bit-identical scalar).
- **Task 20.** `scripts/v0_1g_replay_report.py` + `v0.1g` tag.

### Wave 8 — RCT methodology doc

- **Task 21.** RCT methodology doc (advisory; not a gate).

## Parallelizability note

Wave 6 Tasks 10–18 fan out:

- Tasks 10 + 13 unblock after Tasks 3 + 6 (rubric stub + concrete).
- Tasks 14 + 15 after Task 7 (attachment-risk concrete).
- Task 16 after Task 8 (`recent_shared_moments`).
- Task 17 after Task 9 (user reduction).
- Task 18 after Task 5 (policy hook).

## Numeric gates table

Carry forward all earlier gates. Stage 6 adds:

**Spec gates (Stage 6 — Part 6b):**

| metric                                          | source         |
| ----------------------------------------------- | -------------- |
| `false_proactive_utterances_per_hour`           | Part 6b        |
| `aesthetic_reaction_acceptance_rate`            | Part 6b        |
| `aesthetic_reaction_annoyance_rate`             | Part 6b        |
| `questions_asked_per_hour`                      | Part 6b        |
| `user_reduction_command_compliance_rate`        | Part 6b        |
| `attachment_risk_false_positive_rate`           | Part 6b        |

**Harness-derived (Stage 6):**

| metric                                            | definition                                      |
| ------------------------------------------------- | ----------------------------------------------- |
| `rubric_compliance_rate`                          | % of proposals passing **all 8** checks         |
| `aesthetic_reaction_cooldown_compliance_rate`     | Stage 3 carryforward, Stage 6 enabled           |
| `recent_shared_moments_attribution_rate`          | retrievals citing a real shared-moment item     |
| `thinker_direct_speech_violation_count`           | direct counter; gate `== 0`                     |

For `user_reduction_command_compliance_rate`: **gate-time branching**:
if v0.1f is tagged before v0.1g Task 20 → measure compliance over the
full alphabet; if v0.1f is not yet tagged → measure over
`aesthetic_reaction` only with a follow-up PR re-emitting the metric
post-merge.

## Fixture manifest (17 case_ids)

- `aesthetic_rubric_pass_001`
- `aesthetic_rubric_fail_too_long`
- `aesthetic_rubric_fail_ungrounded`
- `aesthetic_rubric_fail_possessive`
- `aesthetic_rubric_fail_diagnostic`
- `aesthetic_rubric_fail_flattering`
- `aesthetic_rubric_fail_fabricated_memory`
- `aesthetic_rubric_fail_absent_sensory_channel`
- `aesthetic_rubric_fail_identity_only`
- `attachment_risk_high_confidence_001`
- `attachment_risk_prosodic_only_001`
- `user_reduction_less_proactive_001`
- `user_reduction_quiet_mode_001`
- `shared_moment_referenced_001`
- `low_rate_curiosity_hour_001`
- `aesthetic_cooldown_back_to_back_001`
- `thinker_no_direct_speech_001`

## §10 Coordination

v0.1f owns the `proactivity_budget_remaining` budget-key alphabet
(empty set per OQ-6 resolution); v0.1g consumes it trivially.

## §11 Cross-references

- Issue #113 — chain completeness.
- Issue #105 — forget tombstone.
- Issue #107 — analyzer `status_reason`.
- Issue #96 — `no_camera_memory`.

[^1]: see `docs/roadmap-v0.1e-draft.md` §Anchor 2/3 and §Concern C2.

**Operator note (Option C Stage 4):** hybrid duplex+chat-stream is now default (`--use-hybrid` ON); pass `--no-use-hybrid` to revert to duplex-only Path A.
