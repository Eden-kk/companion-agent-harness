# Roadmap — v0.1e (DRAFT — sign-off pending)

> Status: DRAFT (sign-off pending) — generated 2026-05-15 immediately
> after v0.1d feature-completion (commit `a590d82`, tag `v0.1d`).
> Converged through one spec-first reviewer audit and four adversarial
> plan-critic passes (finding counts: 4 BLOCKER → 3 → 2 → 1; pass #4
> explicitly called diminishing returns). Per-task plan-critic passes
> at PR time will handle remaining design-surface items (notably
> OQ-13 — `DuplexModel` Protocol extension signature, locked at Task 11
> PR time). Generated from `architecture-v0.1.md` Part 6
> Stage 4 (lines 514–560 — the four-store memory architecture and nine
> Stage 4 contract tests), Part 5 (`MemoryItem`, `DecisionTrace`,
> `CompanionState`), Part 7 (privacy modes), and the cross-cutting issues:
> **#34 (DecisionTrace production wired at Stage 4)** and
> **#89 (deprecate typed `policy_decision_action_<X>` sub-events once
> DecisionTrace lands)**. Structural template is `docs/roadmap-v0.1d-draft.md`.
> v0.1a (Stage 0 + minimal Stage 1), v0.1b (backchannel-aware EOU), v0.1c
> (Stage 2 audio-video grounding), and v0.1d (Stage 3 speak/silence policy +
> live-loop integration) are all merged and tagged.
>
> The defining design discipline of this milestone — analogous to v0.1d's
> "critical separation" between EOU and speak-policy thresholds — is the
> **provenance invariant (invariant #3)**: every memory item, regardless of
> store, MUST carry `source_event_id`, `created_at`, `confidence`,
> `salience`, `valid_from`/`valid_to`, `superseded_by`, and a
> `user_visible_summary`. No memory write may skip provenance for performance.
> The sleep-time agent's whole reason to exist is that it lets foreground
> stay non-blocking while provenance is computed off the realtime path.

## v0.1e pinned success criterion

> **v0.1e succeeds when the four memory stores hold provenance-bearing items,
> the sleep-time agent commits memory async without blocking foreground p50
> latency, `forget that` invalidates retrieval (tombstone retained),
> `delete that permanently` hard-deletes content (deletion receipt only),
> `why did you say that?` cites a retrievable `DecisionTrace` with
> `retrieval_used` populated, and privacy modes (`no_camera_memory`,
> `guest_present`, `sensitive_conversation`) gate writes correctly — while
> every utterance stays explainable, every policy decision bit-identically
> replayable, and v0.1a–d gates remain green.**

## Anchor decisions — non-transient (treat with extra care)

Most v0.1e design choices are **reversible** behind the `MemoryManager`
Protocol seam (the Protocol is exactly the boundary that makes adapter
swaps cheap). Storage substrate, retrieval algorithm, sleep-time commit
cadence, registry mechanism — all of these can be migrated via a one-time
script or implementation-swap without touching call sites.

**Two choices, however, DO bake into stored data and are NOT cheap to
reverse once memory items begin flowing.** These are the anchor decisions
that warrant plan-critic scrutiny *before* the corresponding tasks begin:

### Anchor 1 — `MemoryItem.content` shape for `semantic_relational`

`MemoryItem` (spec line 259–273) defines `content: dict` opaque to the
schema. The shape *inside* that dict for `semantic_relational` items is
where this anchor lives.

- **Cheap to change:** retrieval algorithm (lexical → embeddings), the
  query-planning code, the index structure.
- **Expensive to change:** the stored shape itself. If items land as
  opaque `{"summary": "..."}` blobs, future relational queries
  (`"what does X like?"`) require either an LLM-assisted parse or a full
  reclassification pass over the durable store. If items land with
  explicit relational structure (subject / predicate / object, plus
  qualifiers), both lexical AND embedding-based retrieval work and the
  data is self-describing.

**Recommendation:** lock the relational shape before Task 9 (the
`semantic_relational` store implementation) begins. Plan-critic should
review the shape proposal; ship Task 9 only after the shape is signed
off. The retrieval *algorithm* (lexical vs embedding) stays a transient
choice behind `MemoryManager.retrieve()`.

### Anchor 2 — `retention_policy_id` value alphabet

`Event.retention_policy_id` (spec line 171) and `SensitiveField.retention_policy_id`
(spec line 113) reference an opaque policy-ID string. The set of IDs
v0.1e stamps onto stored data IS this anchor.

- **Cheap to change:** the TTL or behavior each ID maps to (config-only
  edit; no data migration needed). Adding *new* IDs going forward.
- **Expensive to change:** renaming or removing an existing ID once data
  carries it. Reclassifying existing items (e.g., re-stamping
  `ep_default_30d` items to `episodic_default_v2_30d`) requires a
  one-time pass over the durable stores and a defensible
  legal/UX story for the reclassification.

**Recommendation:** treat policy IDs like stable enum constants —
semantic names, not version-tagged. Lock the v0.1e ID alphabet
**before Task 5 (`episodic_memory` store, the first task that stamps
a `retention_policy_id` onto a `MemoryItem`)**. **Task 2 is the
locking PR** — it lands both the new IDs in Anchor 3 and the
populated `replay_privacy_policy.yaml` registry entries. `DecisionTrace` itself is
not an `Event` and does not carry `retention_policy_id`; the anchor is
about what gets stamped on the events wrapping memory items, not on
DecisionTrace. Anything stamped on a memory item v0.1e is essentially
permanent metadata; the TTL behind the ID is fully tunable forever.

### Anchor 3 — Event payload schemas for new `memory_*` event types

v0.1e introduces new event types: `memory_write_candidate`,
`memory_retrieval_event`, `memory_commit_completed`,
`audit_tombstone_write`, `deletion_receipt_write` (at minimum — final
list locked in Task 2). Every `Event` (spec Part 5 lines 153–171) MUST
carry `payload_kind`, `subject_class`, `sensitivity`, and
`retention_policy_id` — these are required Event fields, not optional.

- **Cheap to change:** the body inside `payload_ref` (opaque pointer per
  spec line 164). Adding *new* event types going forward.
- **Expensive to change:** the four classification axes (`payload_kind`,
  `subject_class`, `sensitivity`, `retention_policy_id`) stamped on
  every event of an existing type. Reclassification would require a
  retroactive pass over the event log AND would invalidate the replay
  privacy policy decisions already made (spec lines 367–381).

**Recommendation:** lock the classification-axes table for v0.1e event
types in **Task 2 (event-type payload schema)**, since DecisionTrace
co-emission introduces the first new event-type bracket. The table:

All values in the `sensitivity` column MUST be one of `schemas.py:57`'s
exact `Literal["safe", "sensitive", "highly_sensitive"]` strings — no
parenthetical annotations (those go in the Notes column).

| event_type | payload_kind | subject_class | sensitivity | retention_policy_id | Notes |
|---|---|---|---|---|---|
| `memory_write_candidate` | `memory_op` | derived from `MemoryItem.subject_class` | derived from `MemoryItem.privacy_level` | derived from Anchor 2 alphabet | per-content classification (subject_class and sensitivity vary per write — set on the wrapper Event from MemoryItem fields) |
| `memory_retrieval_event` | `memory_op` | `self` | `safe` | `retrieval_audit_30d` | query metadata only; retrieved item content lives in separate `MemoryItem` records, not in this event's payload |
| `memory_commit_completed` | `memory_op` | derived from committed item | `safe` | `commit_audit_30d` | receipt only; no content |
| `audit_tombstone_write` | `memory_op` | derived from tombstoned item | `safe` | `audit_indefinite` | forget-receipt; no content per spec line 525-526 ("minimal audit tombstone retained") |
| `deletion_receipt_write` | `memory_op` | derived from deleted item | `safe` | `audit_indefinite` | hard-delete-receipt; no content per spec line 532-533 |

Three new `retention_policy_id` values are coined here:
`retrieval_audit_30d`, `commit_audit_30d`, `audit_indefinite`. These
ARE the v0.1e additions to the Anchor-2 alphabet — the names are
proposed-locked here, not placeholder. TTL values behind each ID are
config (per OQ-4); ID names are durable metadata.

"derived from X" cells mean the actual value is computed from the
referenced field on the wrapped `MemoryItem`, NOT hardcoded — each
`memory_*` event Wrapper is constructed from the item it acts on.
This avoids the misclassification risk of pinning a single
sensitivity to a per-content event type.

**`payload_kind` vs `policy_replay_safe` — clarification (per
researcher pass on plan-critic re-spin Concern C4):** Spec lines
365–381 enumerate `policy_replay_safe` items as specific named data
types and fields — `TurnSignal`, `PolicyInputs (numeric/enum fields
only)`, `SpeakDecision (action_type + reason_code enum)`,
`DecisionTrace.reason_code (enum)`, `DecisionTrace.threshold_path`,
`DecisionTrace.counterfactuals (enum-typed values only)`. The spec
does NOT list `DecisionTrace.retrieval_used` explicitly, nor does it
make `DecisionTrace.*` a blanket class. (Plan-critic re-spin #3
correction: my earlier draft claimed a blanket `DecisionTrace.*`
rule citing spec line 370 — that line is specifically
`DecisionTrace.reason_code`, not a wildcard.)

The spec's `policy_replay_safe` list and `Event.payload_kind` Literal
are **independent classification axes** — neither one determines the
other. What IS classified as `sensitive` (spec line 374-380) is
free-text PII: raw audio/video, transcript text, face/body
descriptors, third-party speech, `DecisionTrace.redacted_explanation`,
and "any free-text annotations on events." Therefore:

- `memory_retrieval_event.payload_kind = "memory_op"` is correct: it
  IS a memory operation in nature.
- `DecisionTrace.retrieval_used` (a `list[str]` of event IDs) is
  policy-replay-safe by virtue of containing **structural
  identifiers** (event IDs), not free-text PII. Event ID strings
  fall under neither the enumerated sensitive items nor the
  free-text-PII catch-all (spec line 380); they are deterministic
  structural keys generated by the harness.
- The retrieval event's payload CONTENT (the retrieved `MemoryItem`)
  is governed by its own `retention_policy_id` — but
  `retrieval_used` only references IDs, which survive Tier B replay
  for the same reason every other structural reference does.

The plan-critic's worry about Tier B replay completeness for
retrieval-informed decisions does not materialize: replay-safety is
about field CONTENT (structural-vs-free-text), not about
`payload_kind` classification of the referenced events.

All three anchors interact with stored data, not just code, which is
why the Protocol seam doesn't fully insulate them. Everything outside
these three boxes is genuinely reversible.

## Adapters / scope delta from v0.1d

- **`DecisionTrace` production wired (issue #34).** `SpeakPolicy.decide()`
  begins co-emitting a `DecisionTrace` alongside the `SpeakDecision`, linked
  by a shared `decision_id`. `DecisionTrace` is the durable audit record
  consumed by Stage 4 explainability (`why did you say that?`), Tier B
  replay, and the analyzer migration in issue #89. **Once
  `DecisionTrace.counterfactuals["action_selected"]` is populated, the
  v0.1d Task 7's latency analyzer migrates off `policy_decision_action_<X>` typed
  sub-events** (issue #89) — the sub-events are removed from
  `realtime_orchestrator.py` and from the analyzer in a single PR after
  DecisionTrace production is stable.
- **`MemoryManager` Protocol implemented.** The v0.1c Stage 3/4 scaffolding
  pass landed `companion_harness/memory_manager.py` with `MemoryManager`
  Protocol + `MemoryManagerStub`. v0.1e replaces the stub with four
  store-backed implementations (`session_state`, `core_user_profile`,
  `episodic_memory`, `semantic_relational`). All four sit behind the same
  Protocol — the seam is preserved; only the implementations change.
- **Sleep-time agent.** New adapter on the **non-realtime path**. Subscribes
  to the event bus (read-only — does not block the orchestrator), batches
  `memory_write_candidate` events, computes provenance fields
  (`confidence`, `salience`, `user_visible_summary`), and commits to the
  appropriate store. Foreground p50 must NOT regress. Adapter-first: model
  SDK access (if any) goes behind a Protocol.
- **`PolicyInputs` extension — TBD.** Stage 4 contract tests may require
  `PolicyInputs` to carry memory-retrieval results back into `decide()` —
  e.g., `retrieved_facts: list[MemoryItem]` for the "why did you say that?"
  loop. **OQ-1:** audit Stage 4 contract tests for fields they demand;
  add **only** genuinely-missing fields; speculative additions are
  forbidden (CLAUDE.md rule 2).
- **Privacy mode gates.** `no_camera_memory`, `guest_present`, and
  `sensitive_conversation` modes already exist as `privacy_mode` strings on
  `PolicyInputs`. v0.1e wires them into the memory-write path: privacy mode
  gates which stores accept writes, **not** which decisions `decide()`
  makes. Adapter-first: privacy gating lives in the `MemoryManager`
  implementation, not in the policy layer.
- **No new external SDKs by default.** v0.1e MAY introduce an embedding
  model adapter for `semantic_relational` retrieval if the Stage 4
  contract tests demand semantic search. **OQ-2:** decide whether to gate
  v0.1e on a real embedding model (b200 inference) or stub semantic
  retrieval with a lexical baseline for the milestone — full semantic
  fidelity is a Stage 6 concern.
- **Stages:** Stage 0/1/2/3 carried (regression gates). **Stage 4 enabled —
  full memory architecture.** Stage 5 (tools), Stage 6 (texture) remain
  disabled.
- **Known spec/implementation divergence (carried, not new):**
  `MemoryItem.user_visible_summary` is `str` in spec (Part 5 line 273)
  but `SensitiveField` in `schemas.py:176`. This is an **intentional
  pre-existing deviation** grounded in the CLAUDE.md project rule that
  all free-text fields go through `SensitiveField` (per the project
  rules in CLAUDE.md, mirroring Part 4's `SensitiveField` discipline
  applied to `companion_state` fields). The deviation is load-bearing
  for invariant #3 (provenance) and was decided pre-v0.1e; v0.1e
  preserves it. Flagging here so future plan-critic passes don't
  re-discover it as a finding.

## §NEXT TASKS (ordered by dependency — one PR, one outcome, per CLAUDE.md rule 4)

Order rationale (per OQ-6 and plan-critic Blocker 2): the
load-bearing pair is DecisionTrace production + `episodic_memory`,
both required by `test_why_did_you_say_that`. Those land first.
Analyzer migration off sub-events depends on DecisionTrace
production. Remaining stores ship after, cheapest-first. The
sleep-time agent depends on at least one store. Privacy gates come
after the agent. Contract tests come last, each as its own PR.

**Parallelizability note (per plan-critic re-spin #4 Nit 6):**
Tasks 13–23 (contract test PRs) share no inter-dependencies among
themselves once their implementation prerequisites land. Specifically:
- Tasks 13, 14, 15 unblock once Tasks 5 + 7 land.
- Tasks 19–22 unblock once Task 12 lands.
- Task 23 unblocks once Task 12 lands.
- Task 18 unblocks once Tasks 4 + 5 + 11 all land.

These can fan out in parallel after their prereqs, reducing
wall-clock time. The numbering is dependency order for clarity,
not strict serial execution.

### Stage 4 schema prerequisites

1. **Add `retrieval_used: list[str]` to `DecisionTrace` dataclass.**
   Plan-critic Blocker 1: the spec test `test_why_did_you_say_that`
   (spec line 545) names `DecisionTrace.retrieval_used` by exact string
   in the expected-behavior description, but the dataclass definition
   at spec line 186-209 and `schemas.py:62-74` omits the field. This
   is a **spec-internal contradiction** (test description names a
   field the type definition lacks). The existing fixture
   `companion_harness/fixtures/why_did_you_say_that_001/case.json` is
   a `"status": "skeleton"` placeholder with no populated
   `retrieval_used` content — the fixture is *awaiting* Stage 4
   activation. (An earlier draft incorrectly claimed the fixture
   already cites `retrieval_used` 3 times; that claim was overstated
   — flagged here as a correction.) Resolution: add the field.
   Recommended signature `retrieval_used: list[str] = field(default_factory=list)`,
   semantically a list of `memory_retrieval_event.event_id` references
   per OQ-8 lean. **Success:** field present, default empty list, no
   v0.1a-d test regressions. `DecisionTrace` is never constructed in
   the codebase today (verified by grep — issue #34 notes production
   is deferred), so the additive change has zero blast radius on
   existing code.

2. **Lock the new event-type payload schema + retention-policy
   registry + memory-state fixture schema sketch** (per Anchor 3).
   One PR landing three interlocked artifacts. **CLAUDE.md rule 4
   defense (per plan-critic re-spin #3 Concern C5):** the three
   artifacts are atomically consistent — anchor-2 retention IDs must
   exist before anchor-3 event-type schema references them, and the
   fixture schema sketch consumes both. Splitting into three PRs
   would land partial states that fail their own integrity checks
   (e.g., a retention ID referenced from a schema before it's
   registered). The ONE outcome of this PR is "v0.1e schema
   declarations are consistent and ready for downstream tasks to
   reference." Concretely:
   - The 5 new `memory_*` event types' classification axes
     (`payload_kind`, `subject_class`, `sensitivity`,
     `retention_policy_id`) in a single registry file.
   - The 3 new `retention_policy_id` entries
     (`retrieval_audit_30d`, `commit_audit_30d`, `audit_indefinite`)
     populated into `companion_harness/replay_privacy_policy.yaml`
     (currently an empty stub). Each entry includes at minimum:
     `id`, `description`, `ttl_days_or_indefinite`. This task also
     defines the registry entry schema itself if not already present.
   - **Memory-state fixture schema sketch** (per plan-critic re-spin
     Concern C5): land a minimal schema declaration for memory-state
     fixtures (pre-populated store snapshots + scripted memory event
     sequences). Need not be exhaustive; just enough that Task 16
     (`correction_001` fixture) doesn't re-open the question.

   No event-emission code in this PR; only schema + registry
   declarations. **Success:** schema file lands; registry YAML has
   3 entries; fixture schema sketch documented; downstream tasks (4
   onward) reference all three.

3. **Apply the `MemoryManager` Protocol change** (resolves OQ-11
   with option (c) — see Closed Decisions section). Rename
   `MemoryManager.write_candidate()` → `MemoryManager.commit()`, with
   foreground emitting `memory_write_candidate` events instead of
   calling the Protocol directly. The `MemoryManagerStub` retains its
   no-op semantics; only method names change. **Files updated in one
   PR:**
   - `companion_harness/memory_manager.py:29` (Protocol method name)
   - `companion_harness/memory_manager.py:38-39` (Stub method name)
   - `tests/test_memory_manager_stub.py:37-39` (test call site —
     update from `write_candidate()` to `commit()` so the test
     continues to assert `NotImplementedError` against the new name)

   **Success:** Protocol, Stub, and test renamed consistently;
   `pytest tests/test_memory_manager_stub.py` passes. **Grep target
   (per plan-critic re-spin #3 Blocker 1):** before the rename, grep
   for any **class** that defines a `write_candidate` method (i.e.,
   Protocol-satisfying classes, not just call sites). Today only
   `MemoryManagerStub` should match. Any class that defines
   `write_candidate` but isn't updated will silently fail
   `isinstance(x, MemoryManager)` post-rename since the Protocol is
   `@runtime_checkable` (`memory_manager.py:27`).

### Stage 4 substrate

4. **Wire `DecisionTrace` production into `SpeakPolicy.decide()`**
   (issue #34). `decide()` returns `SpeakDecision` (unchanged for
   callers) AND co-emits a `DecisionTrace` linked by shared
   `decision_id`. Populate `input_event_ids`, `signal_event_ids`,
   `threshold_path`, `counterfactuals` (including `action_selected`),
   `policy_version`, `config_version`, `model_adapter_versions`,
   `retrieval_used` (empty list until episodic_memory is wired in
   Task 5). **Success:** `test_decision_provenance` (extended) passes
   with a real `DecisionTrace` retrievable by `decision_id`; replay
   match stays 100%. **POLICY_VERSION does NOT bump** here —
   per spec line 202–209 `policy_version` is tied to `decide()`
   behavior changes, not metadata plumbing (plan-critic Concern 7
   confirmed by researcher).

5. **`episodic_memory` store** — durable, append-only with
   bi-temporal indexing. Backs `test_why_did_you_say_that` retrieval
   AND `test_correction` (correction = `valid_to` set on old fact,
   new fact marked active, retrieval excludes old unless history is
   requested). See OQ-4 and Anchor 2 for retention-policy-id
   treatment. Populates `retrieval_used` on the `DecisionTrace`
   emitted by Task 4 (when a retrieval informs a decision). **Success:**
   episodic items retrievable by time-bounded query; `test_correction`
   passes; DecisionTrace's `retrieval_used` list is populated for
   retrieval-informed decisions.

6. **Migrate the v0.1d latency analyzer off typed sub-events** (issue
   #89; the analyzer was built in v0.1d Task 7).
   `companion_harness/live_loop_metrics.py` reads
   `DecisionTrace.counterfactuals["action_selected"]` instead of
   `event.event_type == "policy_decision_action_<X>"`. Remove the 8
   sub-event emissions from `realtime_orchestrator.py`. Update
   fixtures and the live-loop contract test
   (`tests/test_live_loop_latency.py`). **Success:** issue #89
   closed; `pytest tests/test_live_loop_latency.py` green; 4
   NOT_MEASURED latency gates from v0.1d either re-measure or stay
   NOT_MEASURED with the new wiring intact.

### Remaining memory stores

7. **`session_state` store** — in-memory only, no persistence.
   Bi-temporal `valid_from`/`valid_to`; `superseded_by` chains.
   Replaces the stub for session-scope items. **Success:**
   session-scope reads/writes round-trip provenance fields correctly;
   foreground latency unchanged.

8. **`core_user_profile` store** — durable, single-row-per-user
   semantics for stable facts (name, pronouns, role). JSON-on-disk
   with atomic-rename writes (per OQ-3). Provenance fields
   mandatory. **Success:** core profile facts survive process restart.

9. **`semantic_relational` store** — durable, relational structure
   across items (people, places, things, relationships). **Anchor 1
   applies here** — the `MemoryItem.content` shape proposal MUST be
   reviewed by plan-critic before this task begins. Once the
   relational shape is locked, the retrieval algorithm (lexical
   baseline for v0.1e; embeddings = v0.1f+) is a transient choice
   behind `MemoryManager.retrieve()`. **Success:** relational queries
   (e.g., "what does X like?") return provenance-bearing items via
   the locked relational shape.

### Sleep-time agent

10. **`SleepTimeAgent` adapter — background-only.** Subscribes to the
   event bus (subscription mechanism per OQ-12 — see Open Questions),
   batches `memory_write_candidate` events, computes provenance
   fields, commits to stores via `MemoryManager.commit()`. **Hard
   constraint (spec line 541–542):** foreground p50 latency must NOT
   regress with the sleep-time agent active. Adapter-first: any model
   SDK use is behind a Protocol. **v0.1e scope (per SC-1 resolution):**
   the agent is scoped to **memory-store commits only** in v0.1e;
   `companion_state` schema mutation (spec Part 3 line 73) is
   deferred to v0.1f+.

   **Success criterion (per plan-critic re-spin #3 Concern C4 — the
   v0.1d `direct_question_latency_p50` is NOT_MEASURED so an
   absolute-baseline assertion is undefined):** measure foreground
   p50 in the SAME b200 live-loop run with the sleep-time agent
   disabled (control) and enabled (treatment), and assert
   `p50_with_agent ≤ p50_without_agent + tolerance` where `tolerance`
   is a small margin (e.g., 5%) to account for run-to-run noise.
   This is a paired measurement, not an absolute gate. Permanent
   regression-gate test ships in Task 17. The agent's own commit
   cadence is NOT a spec gate — optionally emit
   `memory_commit_completed` events and report cadence in the
   ReplayRun advisory section.

### Cross-adapter retrieval wiring

11. **Wire `MemoryManager.retrieve()` → `ForegroundModel` context path.**
    Plan-critic Concern 6: `test_why_did_you_say_that` cannot pass
    unless retrieval results actually flow to the foreground generation
    path. Per OQ-1, retrieved content does NOT go to `decide()`; it
    goes to the foreground model as system-message context. This task
    defines: (a) what code calls `MemoryManager.retrieve()` and when,
    (b) the `memory_retrieval_event` emitted per the Anchor 3 schema,
    (c) how the retrieved items reach the foreground model's context
    window, (d) how `caused_by[]` closes from `memory_retrieval_event`
    through `policy_decision` to `assistant_audio_buffer_flushed`.

    **Known scope (per plan-critic Concern 2 re-spin):** the existing
    `ForegroundModel` / `DuplexModel` Protocol
    (`companion_harness/foreground_model.py:66-75, 117-136`) has NO
    system-message injection point. This task MUST extend the
    `DuplexModel` Protocol with a context-injection parameter (e.g.,
    `set_context(items: list[MemoryItem])` or a context arg on
    `process_frame`).

    **`@runtime_checkable` consequence (per plan-critic re-spin
    Concern C2):** `DuplexModel` is `@runtime_checkable`
    (`foreground_model.py:65`); `@runtime_checkable` checks method
    existence, not signature. Adding a new required Protocol method
    means every class that doesn't implement it will fail
    `isinstance(x, DuplexModel)` silently. **Before implementing
    Task 11**, grep the codebase for `isinstance(.*DuplexModel)` and
    enumerate every class that satisfies the Protocol (production
    classes + fake-DuplexModel test implementations). Each must be
    updated to implement the new method or its instance check will
    break. Plan-critic should review both the Protocol extension
    proposal AND the migration list before implementation.

    **Success:** end-to-end log of a retrieval-informed utterance has
    a complete causal chain; `DecisionTrace.retrieval_used` references
    the upstream `memory_retrieval_event.event_id`; existing tests
    using fake `DuplexModel` continue to pass with the extended
    Protocol.

### Privacy-mode gates

12. **Privacy-mode gating in `MemoryManager`.** Implements the spec
    Part 7 adapter-compatibility behaviors for `MemoryManager`:
    - `no_memory` — blocks ALL durable writes (most severe; was missing
      from prior draft per plan-critic Blocker 3).
    - `no_camera_memory` — blocks visual-derived memory writes
      (`MemoryManager.episodic.visual_fields` disabled per spec line 826).
    - `guest_present` — blocks durable writes unless explicit-consent
      prompt accepted; `SleepTimeAgent` pauses durable writes
      (spec lines 828–831).
    - `sensitive_conversation` — blocks long-term writes by default
      (opt-in only; spec line 834).

    `local_only` and `child_present` privacy modes (also in spec Part 7
    line 797 enum) are **deferred to v0.1f per OQ-10 + issue #96**.
    Path 1 treatment for v0.1e:
    - **`local_only`** — `MemoryManager.commit()` checks the active
      mode on EVERY call and **raises `NotImplementedError("local_only
      mode is not supported in v0.1e; see issue #96 for the v0.1f
      adapter-routing work")`** if active. The check is
      **action-triggered**, not activation-triggered — a frame carrying
      the mode without an attempted memory write is fine; only an
      actual write call raises. Spec invariant #1 is satisfied (failure
      is explicit and logged via the raised exception, not silent).
      Spec Part 7 lines 811–818 forbid cloud adapters under
      `local_only`; a warn-and-continue would silently violate this,
      which is why hard-raise is required.
    - **`child_present`** — no-op in v0.1e (spec Part 7 has no
      adapter-compatibility table entry for this mode). MemoryManager
      treats it as `normal` for write purposes; no warning, no raise.

    All implemented modes are filters in the
    `MemoryManager.commit()` path (renamed per Task 3 / OQ-11). **Success:**
    `test_no_camera_memory`, `test_guest_present_memory_gate`,
    `test_sensitive_conversation_retention`, `test_no_memory_mode`
    (new), and `test_local_only_mode_raises` (new) all pass.

### Stage 4 contract tests (one PR each — spec lines 519–557)

13. `test_explicit_remember` — user says "remember that I prefer X" →
    visible `memory_write_candidate`, then commit. **Success:** event
    log contains both events; item retrievable.

14. `test_explicit_forget` — user says "forget that" → target
    invalidated for normal retrieval; minimal audit tombstone retained;
    bi-temporal `valid_to` set; `superseded_by` unset (this is the
    distinction from `test_correction` in Task 16 — correction sets
    `superseded_by` to the new item's ID; forget does not). **Success:**
    retrieval skips item; tombstone visible via audit query.

15. `test_explicit_hard_delete` — user says "delete that permanently"
    → hard delete of content where legally/technically possible; only
    non-content deletion receipt retained. **Success:** content
    unrecoverable; receipt present.

16. `test_correction` — exercises the bi-temporal supersession path on
    `episodic_memory` (Task 5). Standalone test PR even though the
    implementation lands with Task 5; one PR, one outcome. **MUST
    flesh out** the existing `correction_001` skeleton fixture
    (`companion_harness/fixtures/correction_001/case.json` exists
    today as `"status": "skeleton"` — plan-critic re-spin #3 Blocker
    2 corrected the prior claim that the fixture was absent). The
    correction case populates `superseded_by` on the old item with
    the new item's `item_id` (distinguishing it from forget, which
    leaves `superseded_by` unset — see Task 14).

17. `test_no_latency_regression` — exercises foreground p50 with
    `SleepTimeAgent` (Task 10) active. Standalone test PR; this is the
    `no_latency_regression_p50/p95` spec gate (Part 6b line 716).
    **Disambiguation (per plan-critic re-spin #4 Nit 5):** Task 10's
    success criterion is the **paired b200 measurement** (manual or
    via inline script, NOT a formal pytest). Task 17 ships the
    **automated pytest** that codifies the paired-measurement
    invariant as a permanent regression guard. Task 10 does NOT
    depend on Task 17 — Task 10 verifies its constraint by direct
    measurement before Task 17 exists.

18. `test_why_did_you_say_that` — exercises the full DecisionTrace
    retrieval loop. Depends on Task 4 (DecisionTrace production),
    Task 5 (`episodic_memory` retrieval), and Task 11 (cross-adapter
    wiring). Standalone PR exercising: user asks, system retrieves
    `DecisionTrace` by `decision_id`, cites the upstream
    `memory_retrieval_event` via `retrieval_used` and the
    policy_decision via `caused_by[]`. **MUST** flesh out the
    `why_did_you_say_that_001` fixture, which is currently a
    `"status": "skeleton"` placeholder with empty frames/ops
    (plan-critic re-spin Concern 4 — the fixture grounding I previously
    claimed for this test was overstated; the fixture is a skeleton,
    not a populated trace).

19. `test_no_camera_memory` — exercises the `no_camera_memory` privacy
    mode behavior per spec Part 7 lines 823–827 (VisionSidecar fields
    disabled in episodic store).

20. `test_guest_present_memory_gate` — exercises the `guest_present`
    privacy mode behavior per spec Part 7 lines 828–831
    (SleepTimeAgent pause durable writes; MemoryManager consent
    prompt).

21. `test_sensitive_conversation_retention` — exercises the
    `sensitive_conversation` privacy mode behavior per spec Part 7
    lines 833–836 (long_term_writes disabled by default).

22. `test_no_memory_mode` (new — added per Blocker 3) — exercises the
    `no_memory` privacy mode behavior: ALL durable writes blocked
    while the mode is active; `memory_write_candidate` events MAY be
    emitted but MUST NOT commit. **Success:** mode-active session
    produces zero `memory_commit_completed` events.

23. `test_local_only_mode_raises` (new — added per Path 1 resolution
    of plan-critic re-spin Blocker B2-new) — exercises the
    `local_only` privacy mode hard-raise behavior. Setting
    `privacy_mode="local_only"` and then attempting a memory commit
    must raise `NotImplementedError` referencing issue #96.
    **Success criteria (all 3 must hold):**
    (a) the raise fires on commit attempt under `local_only`;
    (b) mere mode activation without a commit attempt does NOT raise
        (action-triggered, not activation-triggered);
    (c) the exception message contains the literal string `#96` (so
        future drift of the message text catches a regression instead
        of silently dropping the issue reference).

### Replay determinism + report

24. **Extend `test_policy_replay_exact` for DecisionTrace co-emission
    determinism.** Per OQ-1, `decide()` does NOT begin reading new
    `PolicyInputs` fields at v0.1e; the existing Stage 3 replay
    continues to hold. The extension is: assert `DecisionTrace`
    co-emission is deterministic (same `decision_id` chain, same
    `counterfactuals` payload, same `retrieval_used` list across
    repeated calls with identical inputs). **POLICY_VERSION does NOT
    bump** — per spec line 202–209 and plan-critic Concern 7,
    `policy_version` is tied to `decide()` behavior changes
    (different inputs → different outputs), not to schema/metadata
    plumbing changes. DecisionTrace production is infrastructure
    around `decide()`, not policy logic. Mirror the v0.1d Task 16
    test-shape pattern; do NOT mirror its version bump.

25. **v0.1e local gate verification + ReplayRun report + tag
    `v0.1e`.** `scripts/v0_1e_replay_report.py` mirrors the v0.1d
    sibling. Gate table = the 5 spec Part 6b Stage 4 gates + carried
    v0.1a/b/c/d gates + 3 harness-derived gates. v0.1d's 4
    NOT_MEASURED latency gates SHOULD be re-measured on b200 with the
    sleep-time agent active (sleep-time async is the most plausible
    source of foreground latency regression — if b200 access is not
    available before tag time, gates stay NOT_MEASURED with reason
    "requires b200 sleep-time-agent active run"; this is honest, not a
    failure). Tag applied by project lead.

## v0.1e numeric-gates table (added / carried)

The 5 spec Part 6b Stage 4 metric names (lines 711–718) are the
authoritative gate names. Use them verbatim in the ReplayRun report so
gate-name greps work across milestones. Harness-derived gates (where the
contract test demands behavior that maps to no single Part 6b metric)
are marked **harness-derived** to distinguish them from spec gates.

| Metric | Gate | Source |
|---|---|---|
| `explicit_remember_compliance_rate` | = 100% on fixture set | spec Part 6b line 713 |
| `explicit_forget_compliance_rate` | = 100% on fixture set | spec Part 6b line 714 |
| `correction_supersession_rate` | = 100% on fixture set | spec Part 6b line 715 |
| `no_latency_regression_p50/p95` | `p50_with_agent ≤ p50_without_agent + 5% tolerance` (paired measurement in same b200 run — see Task 10) | spec Part 6b line 716; tested via `test_no_latency_regression`. NOT an absolute-baseline gate since v0.1d's `direct_question_latency_p50` is NOT_MEASURED. |
| `privacy_mode_compliance_rate` | = 100% on fixture set | spec Part 6b line 717. Covers 4 of 7 spec privacy modes: `no_memory` (via `test_no_memory_mode`), `no_camera_memory` (via `test_no_camera_memory`), `guest_present` (via `test_guest_present_memory_gate`), `sensitive_conversation` (via `test_sensitive_conversation_retention`). `local_only` and `child_present` deferred per OQ-10. Note: `test_local_only_mode_raises` (Task 23) verifies the v0.1e hard-raise behavior for `local_only` but does NOT contribute to this gate — it covers a harness-derived deferral-correctness check, not spec compliance. |
| `hard_delete_content_unrecoverability_rate` | = 100% on fixture set | **harness-derived** — `test_explicit_hard_delete` (spec line 529–534) has no direct Part 6b metric; gate name is implementation-chosen. **Per Anchor 2 reasoning (per plan-critic re-spin #4 Concern 3): harness-derived gate names ARE durable metadata once ReplayRun reports are landed.** Treat the 3 harness-derived names (`hard_delete_content_unrecoverability_rate`, `why_did_you_say_that_trace_retrieval_rate`, `decision_trace_co_emission_rate`) as stable enum constants — renaming after v0.1e ReplayRun runs land requires retroactive reclassification across all stored reports. Lock these names in Task 25 (ReplayRun report). |
| `why_did_you_say_that_trace_retrieval_rate` | = 100% on fixture set | **harness-derived** — `test_why_did_you_say_that` (spec line 544–545) has no direct Part 6b metric |
| `decision_trace_co_emission_rate` (every `policy_decision` event has a paired retrievable `DecisionTrace`) | = 100% | **harness-derived** — invariant #1 + #5 check on DecisionTrace production |
| All v0.1a/b/c/d gates | unchanged | carried — must remain green |
| `direct_question_latency_p50 / p95` | < 800 ms / < 1500 ms | carried — re-measure on b200 with sleep-time agent active (Task 25) |
| `vad_detected_user_speech_to_stop_ms_p95` | < 200 ms | carried |
| `false_interruption_count_per_10_min` | < 1 | carried |

**Eval (advisory, NOT a gate):** LoCoMo, LongMemEval, MemoryAgentBench
named at spec line 560; spec Part 6b line 689 explicitly frames public
benchmarks as "secondary regression probes… not the gate." These
measure memory *quality* and are reported in the **advisory section**
of the v0.1e ReplayRun report, never in the gate section.

## Carryforward from v0.1d

- **4 NOT_MEASURED latency gates** from v0.1d (`direct_question_latency_p50/p95`,
  `vad_detected_user_speech_to_stop_ms_p95`, `false_interruption_count_per_10_min`)
  require a b200 live-loop run. v0.1e SHOULD re-measure these once the
  sleep-time agent is wired — sleep-time async work is the most likely
  source of foreground latency regression, so we want a fresh number.
- **Issue #89 typed sub-events deprecation** — closes when Task 6 (analyzer
  migration) lands.
- **Task 9b deferred `tool_status` branch** from v0.1d is Stage 5 work, NOT
  v0.1e. Stays deferred.

## Open questions for the project lead

Each OQ is tagged with whether it is **spec-bound** (the spec dictates the
answer), **spec-anchored** (spec provides a partial anchor; remainder is
implementation choice), or **spec-silent** (spec doesn't constrain;
recommendation is engineering choice, not a spec consequence). This
framing follows the spec-first / coding-principles-second rule applied
during the post-draft reviewer audit.

- **OQ-1 — CLOSED.** See Closed Decisions section below.

- **OQ-1 [archived rationale, retained for traceability]:** Does `PolicyInputs` need
  `retrieved_facts: list[MemoryItem]`? **NO.** Spec Part 5 line 219–233
  enumerates `PolicyInputs` with 14 fields; `retrieved_facts` is not one
  of them. `test_why_did_you_say_that` (spec line 544–545) cites
  `retrieval_used` in the *trace*, not in `PolicyInputs`. **The retrieval
  linkage is `DecisionTrace.retrieval_used`** — a list of
  `memory_retrieval_event.event_id` references, added as a field in
  Task 1. Spec internal contradiction: spec line 545 names the field
  by exact string ("expected: cites retrieval_used + policy_decision
  in trace"), but the `DecisionTrace` dataclass at spec line 186-209
  and `schemas.py:62-74` does NOT include `retrieval_used`. The
  existing fixture `companion_harness/fixtures/why_did_you_say_that_001/case.json`
  is a `"status": "skeleton"` placeholder — it does NOT currently
  cite `retrieval_used` (correcting the overstated fixture grounding
  in the previous draft revision; the fixture is empty pending Stage 4
  activation). Resolution: add the field per Task 1; populate when
  retrieval is wired (Task 5 + Task 11). Retrieved content itself goes
  to `ForegroundModel` (separate adapter, Task 11), not to
  `decide()`. If a future contract test demands retrieval-aware policy
  gating, the right shape is a signal field
  (`memory_retrieval_succeeded: bool`) consistent with PolicyInputs'
  signal-not-content idiom — but that is a hypothetical and not a
  v0.1e scope item.

- **OQ-2 [spec-silent on algorithm; spec-anchored on data shape]:**
  Real embeddings vs lexical baseline for `semantic_relational` retrieval?
  Spec is silent on the retrieval algorithm. Spec Part 6b line 689
  explicitly frames public benchmarks (LoCoMo, LongMemEval,
  MemoryAgentBench, named at line 560) as "secondary regression probes…
  not the gate." The 5 Stage 4 mechanism-level metrics (Part 6b lines
  711–718) require none of them. **Algorithm = transient implementation
  choice** behind `MemoryManager.retrieve()`; lexical baseline is
  defensible and faster to ship. **Data shape = anchor decision** (see
  Anchor 1 above) — lock the relational shape of `MemoryItem.content`
  before Task 9 begins.

- **OQ-3 [spec-silent]:** `core_user_profile` substrate? Spec is
  storage-agnostic — `MemoryItem` (line 259–273) defines provenance
  fields, not a substrate. This is a pure implementation choice; the
  spec neither mandates nor forbids any particular substrate.
  **Recommendation (engineering, not spec-derived):** JSON-on-disk with
  atomic-rename writes. Reversible via a one-time migration script if
  v0.1f+ surfaces a need for SQLite (transactional cross-store updates,
  multi-row consistency).

- **OQ-4 [spec-silent on defaults; spec-anchored on identifier scheme]:**
  `episodic_memory` retention defaults? Spec ties retention to
  `retention_policy_id` references (Event line 171, SensitiveField line
  113) but does NOT pin TTL values. The ONLY numeric retention figure
  anywhere in the spec is `raw_media_retention.default_seconds: 300`
  (spec line 351–381, Replay Privacy Policy) — and that applies to raw
  media, not memory items. **Defaults = spec-silent**, an engineering
  choice. **Identifier alphabet = anchor decision** (see Anchor 2 above)
  — once memory items carry a `retention_policy_id` value, that name
  becomes durable metadata. Lock the v0.1e ID alphabet during the
  DecisionTrace production task.

- **OQ-5 [spec-bound on what to gate; spec-silent on the agent's own
  cadence]:** Sleep-time agent commit-latency bound? Spec line 516
  ("Sleep-time agent mutates async — foreground never blocks") and the
  `test_no_latency_regression` contract (line 541–542, "memory
  operations do not increase foreground p50 response time") gate
  **foreground p50 only**. Spec pins no commit-latency-of-the-agent
  number. **Removed from open questions** — no v0.1e gate on this.
  Optionally, emit `memory_commit_completed` events and report
  commit-latency in the ReplayRun **advisory section** (non-gated),
  matching v0.1d's Stage 6 advisory pattern.

- **OQ-6 — CLOSED.** See Closed Decisions section below.

- **OQ-6 [archived rationale]:**
  Memory-store PR order? Spec lines 544–545 (`test_why_did_you_say_that`)
  require both DecisionTrace + `episodic_memory` retrieval — making
  these the most load-bearing pieces. Issue #34's spec-research note
  confirms DecisionTrace becomes load-bearing AT Stage 4 because of this
  test. **Spec anchor:** front-load DecisionTrace + episodic_memory.
  Remaining order (`session_state` → `core_user_profile` →
  `semantic_relational`) is a project-management inference from
  complexity / dependency, not spec-mandated. Recommendation: (1) DecisionTrace
  production, (2) episodic_memory store, (3) analyzer migration off
  sub-events, (4) session_state, (5) core_user_profile, (6) semantic_relational.

- **OQ-7 — CLOSED.** See Closed Decisions section below.

- **OQ-7 [archived rationale]:** Bundle
  DecisionTrace production + analyzer migration, or split? **SPLIT.**
  Spec invariant #5 (line 48) — "Policy-layer replay must be
  deterministic" — means DecisionTrace production (a new logged
  side-effect on `decide()`) must be verified to not break Tier B replay
  before *any* downstream consumer migrates. The split lets us assert
  `policy_replay_match_rate` stays 100% after PR #1, then verify the
  v0.1d Task 7's latency analyzer still computes correctly after PR #2. The
  v0.1d Task 7 prereq+main precedent is **project history** that
  supports this cadence — it is not itself a spec citation, but the
  spec invariant #5 grounding is.

- **OQ-8 [spec-silent — added per researcher pass on Blocker 1]:**
  `DecisionTrace.retrieval_used` content semantics — event IDs or item
  IDs? Researcher recommends `list[str]` of
  `memory_retrieval_event.event_id` references (event-based).
  Alternative: `list[str]` of `MemoryItem.item_id` references
  (content-based). Spec is silent on shape (line 545 names the field
  but specifies no type). **Lean: event-based** — this composes with
  invariant #1's causal-graph closure (the event has its own
  `caused_by[]` and traceability), whereas an item-ID list would
  require a separate lookup step to recover when the retrieval
  happened. The invariant-#1 grounding is composition with the
  causal graph, NOT a direct mandate. Confirm before Task 1 begins.

- **OQ-9 — CLOSED.** See Closed Decisions section below.

- **OQ-10 [spec-silent on deferral; spec-anchored on coverage] —
  added per plan-critic Blocker 3:** Privacy modes `local_only` and
  `child_present` deferred to v0.1f? Spec Part 7 line 797 enumerates
  7 privacy modes; spec Part 7 lines 805–836 specifies adapter
  behaviors for `local_only`, `no_camera_memory`, `guest_present`,
  `sensitive_conversation`. `child_present` has NO Part 7
  adapter-compatibility table entry. **Lean: defer `local_only` (its
  adapter-routing complexity is v0.1f scope — Part 7 lines 811–818
  list cloud-adapter prohibitions that extend beyond MemoryManager)
  and `child_present` (no spec-mandated behavior beyond the enum
  presence).** Confirm the deferral, OR fold them into Task 12 with
  spec-grounded behaviors.

- **OQ-13 [spec-silent — added per plan-critic re-spin #4 Concern 2]:**
  `DuplexModel` Protocol extension signature for context injection
  (Task 11)? Options:
  (a) `set_context(items: list[MemoryItem]) -> None` — explicit
  setter, decoupled from per-frame call;
  (b) add `context: list[MemoryItem] | None = None` parameter to
  `process_frame()` — frame-scoped, no state across calls;
  (c) new method `inject_system_message(message: str) -> None` —
  text-formatted, model decides how to consume.
  **Lean: (a)** — explicit, stateful between context updates,
  easiest to mock in fake-DuplexModel tests. Confirm and lock the
  signature in a dedicated per-task plan-critic pass on Task 11
  before implementation begins. This OQ is the single design item
  the whole-roadmap plan-critic flagged as warranting targeted
  per-task adversarial review.

- **OQ-12 [spec-silent — added per plan-critic Concern 8]:**
  `SleepTimeAgent` event-bus subscription mechanism? Spec Part 3
  line 73 + Part 6 line 516 say the agent is async, but no
  pub-sub interface exists in the codebase. Options:
  (a) poll the `EventLogger`'s on-disk JSONL output (simple, no
  coupling to orchestrator);
  (b) add an asyncio queue to `RealtimeOrchestrator` that the agent
  consumes (tighter coupling but lower-latency);
  (c) `EventLogger` exposes a subscribe interface (clean separation
  but new infrastructure).
  **Lean: (a)** — minimum new infrastructure, decouples sleep-time
  agent from realtime path entirely (invariant #10 protection by
  construction).
  **Known risk for option (a) (plan-critic re-spin Concern C3):**
  partial-line race — if the agent reads JSONL while EventLogger is
  mid-write, it may consume an incomplete line. Mitigation: the agent
  reads only **newline-terminated lines** (skips trailing partial
  buffer until next read tick) AND maintains a **byte-offset
  checkpoint** so it never re-reads completed lines. If this
  mitigation is judged insufficient by Task 10's plan-critic, fall back
  to option (c).

  **Architectural assumption for option (a) (plan-critic re-spin
  Concern C1, refined per re-spin #3 Concern C6):** `EventLogger`
  uses an `asyncio.Queue` + injected `Sink` callable
  (`event_logger.py:18-31`), NOT a built-in JSONL-on-disk writer. **A
  JSONL-file sink does not currently exist anywhere in the
  codebase** — option (a) implies a **new production sink artifact**
  (a `JSONLFileSink` callable that the production runtime injects
  into `EventLogger`). That new sink is a separate sub-task within
  Task 10, not a free dependency. If Task 10's plan-critic judges
  the new-sink scope too large, fall back to option (c)
  (`EventLogger.subscribe()` API) which doesn't require persisting
  to disk. Either way, the choice is a real Task 10 design decision
  with a real implementation cost.

  Confirm before Task 10 begins.

- **SC-1 resolution (per plan-critic):** Part 3 line 73 names
  `SleepTimeAgent` as "async companion_state mutator"; Part 6 line
  516 says it handles memory commits; Part 9 line 984 instantiates
  it as a memory-extraction model. **Interpretation:** Part 3's
  "async companion_state mutator" is the **overarching role**;
  Part 6's memory commit is the **v0.1e instantiation** of that
  role; `companion_state` schema mutation (Part 4) is a later
  sub-role (v0.1f+). The plan scopes v0.1e Task 10 to memory commits
  only; `companion_state` mutation explicitly deferred. This
  interpretation must be ratified before Task 10 begins to avoid
  re-litigation.

## Closed decisions

Decisions resolved during plan-critic / reviewer convergence; moved
here from "Open questions" once locked. Recorded so future passes
don't re-litigate.

- **OQ-9 — RESOLVED to empty list + `default_factory=list`.** Task 1
  prescribes the signature `retrieval_used: list[str] = field(default_factory=list)`.
  The `None`-default alternative was rejected as more brittle.
  Task 4 review checklist (DecisionTrace production) confirms all
  decide() code paths populate the field when retrieval is active.

- **OQ-11 — RESOLVED to option (c) — event-bus pattern, Protocol
  rename.** Foreground emits `memory_write_candidate` events
  (no direct Protocol call); the sleep-time agent consumes the events,
  constructs full `MemoryItem` instances with provenance fields
  populated, and calls `MemoryManager.commit(item)` (renamed from
  `write_candidate`). Reasoning:
  - Keeps `MemoryItem` non-Optional, satisfying invariant #3
    (provenance fields mandatory).
  - Matches Part 6 line 516 ("foreground never blocks") by design —
    foreground does no synchronous Protocol call.
  - Fits the event-bus pattern the rest of v0.1e uses.
  - Spec-silent on this decision, but option (c) was the lean
    consistent with multiple spec invariants.

  Tasks 5, 10, 12, 23 reference `MemoryManager.commit()` accordingly.
  Task 3 implements the Protocol/Stub/test rename in one PR.

- **OQ-1 — RESOLVED: NO `retrieved_facts` field on `PolicyInputs`.**
  Spec Part 5 line 219–233 enumerates `PolicyInputs` with 14 fields;
  `retrieved_facts` is not one of them. `test_why_did_you_say_that`
  (spec line 545) cites `retrieval_used` in the trace, not in
  `PolicyInputs`. The retrieval linkage is `DecisionTrace.retrieval_used`
  (added in Task 1, populated by Task 5 via Task 11's
  cross-adapter wiring). Retrieved content goes to `ForegroundModel`
  via system-message context (Task 11), not to `decide()`.

- **OQ-6 — RESOLVED: PR order is** (1) `retrieval_used` schema add,
  (2) event-type schema + retention registry + fixture schema (Anchor 3),
  (3) MemoryManager Protocol rename, (4) DecisionTrace production,
  (5) episodic_memory, (6) analyzer migration, (7) session_state,
  (8) core_user_profile, (9) semantic_relational, (10) SleepTimeAgent,
  (11) cross-adapter retrieval wiring, (12) privacy gates, (13–23)
  contract tests, (24) replay extension, (25) ReplayRun report + tag.
  Front-loads DecisionTrace + episodic_memory per spec line 544–545
  load-bearing test. Order locked.

- **OQ-7 — RESOLVED: SPLIT.** DecisionTrace production (Task 4)
  and analyzer migration (Task 6) ship as separate PRs. Spec
  invariant #5 (deterministic policy replay) means DecisionTrace
  production must be verified to not break Tier B replay before
  any downstream consumer migrates. The split lets us assert
  `policy_replay_match_rate` stays 100% after Task 4, then verify
  the latency analyzer still computes correctly after Task 6.
  Mirrors the v0.1d Task 7 prereq+main precedent (project history,
  not a spec citation, but spec invariant #5 grounds the split).

## Contract-test stages

The ten Stage 4 tests above (including new `test_no_memory_mode`) are
all **Stage 4 (Memory)**. Stage 0–3 tests continue passing as regression
gates. **`test_decision_provenance` is extended** in Task 4 to assert a
real `DecisionTrace` is produced (with `retrieval_used` populated when
applicable) and retrievable by `decision_id`. Every v0.1e PR's verify
step re-runs the full suite.

**Fixture representation:** v0.1e fixtures introduce **memory-state
fixtures** — pre-populated store snapshots (JSON) and scripted memory
event sequences. Pattern is TBD; mirror Stage 3 per-frame `PolicyInputs`
traces where applicable.

---

**End of draft — sign-off pending. Plan-critic convergence loop closed (4 passes; verdict: diminishing returns).**
