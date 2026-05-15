# Roadmap — v0.1f (DRAFT)

## Status: **DRAFT** — converged 2026-05-15 (recovered from shared-worktree race).

> Converged through plan-critic R1 (3 BLOCKERs + 11 CONCERNs + 6 NITs applied)
> and R2 (1 CONCERN + 3 NITs, 2 applied). Original draft was lost in a
> shared-working-tree race between concurrent fix-coder agents; this file
> reconstructs the converged content from agent notes.
>
> v0.1f scope is **Stage 5 — Background reasoning & tool routing** from
> `docs/architecture-v0.1.md` Part 6. v0.1a (Stage 0 + minimal Stage 1),
> v0.1b (backchannel-aware EOU), v0.1c (Stage 2 audio-video grounding),
> v0.1d (Stage 3 speak/silence policy + live-loop integration), and v0.1e
> (Stage 4 four-store memory + sleep-time agent) are all merged and tagged.
>
> The defining design discipline of this milestone is **invariant #9**:
> no foreground narration about tool progress may exist without a
> corresponding `ToolProgressEvent` in the event log. Invented progress
> in voice is more manipulative than in text; the spec carries this
> invariant precisely because the texture (Stage 6) compounds the harm.

## v0.1f pinned success criterion

> **v0.1f succeeds when the system handles long-running tool calls without
> foreground blocking; tool calls cancel within 300ms on barge-in;
> foreground narration about tool progress is evidence-bound (only when a
> `ToolProgressEvent` exists in the log); filler budget is enforced (max
> 2 fillers per call, 4 seconds between, silence-wins-after-first-filler);
> v0.1e gates (memory) and all earlier gates remain green.**

## Anchor decisions — non-transient (locked)

Most v0.1f design choices are reversible behind the `ToolRouter` Protocol
seam. The four anchors below are the choices that bake into stored data
or contract-test surfaces and are NOT cheap to reverse.

### Anchor 1 — `ToolProgressEvent` payload shape + locked `progress_stage` alphabet

`progress_stage` is a `Literal["started", "scanning", "aggregating",
"completed", "cancelled"]`. **Locked alphabet** — adding values is a
future-spec change, not a v0.1f task.

Note: `ToolProgressEvent` (PascalCase, spec naming) is concretely an
`Event` with `event_type='tool_progress_event'` (snake_case). No new
dataclass is introduced — Anchor 1 governs the payload schema only.

### Anchor 2 — 5 new `tool_*` event types + 2 new `retention_policy_id` values + ordering invariant

New event types (snake_case `event_type` values):

- `tool_call_requested`
- `tool_call_dispatched`
- `tool_progress_event`
- `tool_call_completed`
- `tool_call_cancelled`

All carry `tool_call_id: str` in payload (deterministic UUID generated
by `ToolRouter.dispatch()`).

**Ordering invariant:** chain closes via `caused_by[]`:

```
tool_call_requested
    → tool_call_dispatched
        → tool_progress_event*  (zero or more)
            → (tool_call_completed | tool_call_cancelled)
```

New `retention_policy_id` values:

- `tool_call_audit_30d`
- `tool_result_default`

### Anchor 3 — `routing_tier` recorded as a field on `tool_call_dispatched`

`routing_tier: Literal["fast", "smart"]` is a field on the
`tool_call_dispatched` payload. Replay determinism requires a single
recorded value, not a fallback chain.

### Anchor 4 — Filler-budget state machine on `ToolProgressEmitter`

Per-tool-call filler-budget state machine owned by `ToolProgressEmitter`;
surfaced to policy via `PolicyInputs.tool_progress_evidence` field.
POLICY_VERSION bumps from `v0.1d` → `v0.1f` in Task 5.

**Replay determinism contract:** `ToolProgressEmitter` MUST derive
`ms_since_last_filler` from `timestamp_mono_ms` deltas between
`tool_progress_event`s in the event log, never from a live wall-clock
read. Exposes a pure `evidence_at(tool_call_id, now_mono_ms)` form for
replay.

## Closed decisions (OQs 1–13, all RESOLVED 2026-05-15)

- **OQ-1**: Fake-tool harness at v0.1f (real MCP deferred to v0.1g).
- **OQ-2**: `ToolProgressEvent.caused_by` = immediate predecessor +
  graph-traversal closure to user-utterance signal.
- **OQ-3**: Background-reasoner budget = wall-clock only at v0.1f
  (e.g., 30s/call).
- **OQ-4**: Barge-in tool-cancellation extends existing
  `cancel_generation` path.
- **OQ-5**: Stage 5 expands `SpeakPolicy` for `tool_status` (spec line 238).
- **OQ-6**: Commit to one path per tool call (no smart-path fallback).
- **OQ-7**: Reuse `set_context()` for smart-path summary injection (no
  new Protocol method).
- **OQ-8**: Per-tool-call filler budget.
- **OQ-9**: Filler budget separate from `cooldown_state` (own
  `tool_progress_evidence` field).
- **OQ-10**: Cancel scope at v0.1f = barge-in only.
- **OQ-11**: `local_only` hard-raises `NotImplementedError` on
  `ToolRouter.dispatch()`.
- **OQ-12**: Smart-path reasoner subscribes via asyncio queue from
  orchestrator.
- **OQ-13**: `tool_status` (during) vs context injection (after) are
  distinct surfaces, not a contradiction.

## Tasks (15 tasks, atomic-merged Task 1+2 from R1 patch)

### Wave 1 — Foundation (schema, event types, reason codes)

- **Task 1 (atomic merger).** Ships 5 tool_* event types in
  `v0_1f_event_schema.py`; `progress_stage` Literal alphabet in
  `tool_progress.py`; `ToolProgressEvidence` dataclass;
  `PolicyInputs.tool_progress_evidence` field; `tool_call_id` field on
  all event payloads; 2 new ReasonCodes
  (`TOOL_FILLER_BUDGET_EXHAUSTED`, `TOOL_PROGRESS_EVIDENCE_MISSING`);
  2 new retention IDs.
  **Success:** `pytest tests/test_v0_1f_event_schema.py -v` passes.
  **(Already shipped: PR #128.)**

### Wave 2 — Fast-path substrate + policy + orchestrator wiring

- **Task 2.** `ToolRouter` Protocol + fake adapter.
  **Success:** `pytest tests/test_tool_router_fake.py -v` passes.
- **Task 3.** `FastToolDispatcher` concrete.
  **Success:** `pytest tests/test_fast_tool_dispatcher.py` passes.
- **Task 4.** `ToolProgressEmitter` (per-tool-call filler-budget state
  machine; pure `evidence_at()`).
  **Success:** `pytest -k filler_budget_state_machine` passes including
  replay-determinism via `evidence_at()`.
- **Task 5 (speak_policy wiring).** Wire `tool_status` into
  `SpeakPolicy.decide()`. POLICY_VERSION literal bumped
  `v0.1d → v0.1f`; every fixture pinning `policy_version` updated;
  `test_policy_replay_exact` still passes. ADR cleanup: if
  `# tool_status: deferred to Stage 5` comment exists in
  `speak_policy.py`, remove it (verified absent 2026-05-15, no-op kept
  for traceability). Close v0.1d task 9b ledger entry.
- **Task 6 (orchestrator wiring).** End-to-end fast-path.
  **Success:** `tests/test_tool_router_orchestrator_wiring.py::test_fast_path_full_lifecycle`.

### Wave 3 — Cancellation + smart path

- **Task 7 (cancellation).** Barge-in extends `cancel_generation` to
  call `tool_router.cancel(tool_call_id)`.
  **Success:** `tests/test_tool_router_cancel.py::test_barge_in_cancels_in_flight_tool`.
- **Task 8 (smart path).** `BackgroundReasoner` Protocol +
  `FakeBackgroundReasoner` (no recursive `ToolRouter` calls; that's
  v0.1g). Signatures:
  `select_and_call(request: ToolDispatchRequest) -> AsyncIterator[Event]`,
  `summarize(results: ToolReasonerResult) -> list[MemoryItem]`.
  Reasoner yields `Event`s; orchestrator forwards to
  `EventLogger.log()` (invariant #10).
- **Task 9.** Smart-path orchestrator wiring. Asyncio queue from
  `RealtimeOrchestrator` (OQ-12).
  **Success:** `tests/test_background_reasoner_wiring.py::test_smart_path_context_injection`.

### Wave 4 — Contract tests

- **Task 10.** `test_no_foreground_block` with paired-latency gate:
  `p50_during_tool_call ≤ p50_no_tool_call + 5%` tolerance in same b200
  run (mirrors v0.1e Task 10).
- **Task 11.** `test_cancellation_on_barge_in` (300ms p50 gate per spec
  line 588).
- **Task 12.** `test_filler_evidence_bound`.
- **Task 13.** `test_filler_specificity_with_evidence`.

### Wave 5 — Replay extension + report + tag

- **Task 14 (replay extension).** `test_policy_replay_exact` extended
  for Stage 5 inputs. Asserts replay determinism via `evidence_at()`
  with recorded event-log fixture. POLICY_VERSION asserted at `v0.1f`.
- **Task 15 (ReplayRun report + tag).** `scripts/v0_1f_replay_report.py`.
  Tag `v0.1f` after merge.

### Parallelizability note (post-renumber)

Tasks 10–13 (contract tests) fan out once Tasks 4+5+6 land; Task 10
also needs Task 7; Task 14 needs Task 5 (POLICY_VERSION bump). Tasks
8+9 (smart path) parallel to Tasks 6–7.

## Numeric gates table

Carry forward v0.1a/b/c/d/e gates verbatim. Stage 5 adds:

**Spec gates (Stage 5 — Part 6 / spec line 588 region):**

| metric                                       | gate                                  |
| -------------------------------------------- | ------------------------------------- |
| `foreground_block_count_per_session`         | == 0                                  |
| `tool_cancellation_latency_ms_p50`           | < 300ms (worst-case bound, spec line 588 silent on percentile) |
| `filler_evidence_bound_compliance_rate`      | == 1.0                                |

**Harness-derived (Stage 5):**

| metric                                  | gate    |
| --------------------------------------- | ------- |
| `tool_progress_attribution_rate`        | == 1.0  |
| `filler_budget_compliance_rate`         | == 1.0  |
| `tool_call_caused_by_closure_rate`      | == 1.0  |

## §10 Coordination notes

- **`proactivity_budget_remaining` alphabet:** v0.1f adds **NO new keys**.
  Filler budget lives in `PolicyInputs.tool_progress_evidence` per
  Anchor 4. v0.1g hard-dependency satisfied trivially.
- **Live-loop integration:** tool routing fixture-replay-tested per
  v0.1e pattern; no live-loop coupling required.
- **`DuplexModel` Protocol:** reuse `set_context()` per OQ-7; no new
  method.

## §11 Cross-references

- Issue #113 — chain completeness (`caused_by[]` closure).
- Issue #96 — `local_only` v0.1f hard-raise (OQ-11).
- Issue #107 — analyzer `status_reason`.
- Issues #34 / #89 — closed at v0.1e.
