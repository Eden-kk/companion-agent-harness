# Plan — Dashboard model-picker (hot/cold seams, 3-phase rollout) — DRAFT

## Status

**DRAFT — 2026-05-15. Pre-plan-critic.** Convention follows other `plan-*-draft.md`
docs in this directory (e.g. `plan-eval-console-pr1-draft.md`,
`plan-memory-wiring-followup.md`, `plan-vision-sidecar-wiring.md`). Will go
through `/plan-review` before any coder picks it up.

This is a *design* doc. No code changes are proposed in this PR — only a frozen
set of anchor decisions, an open-question (OQ) ledger with leans, and a phased
rollout that future plan-execution PRs will consume one phase at a time.

---

## Why this is needed

The manual-test console (`manual_test_console/server.py` + `index.html`) is
where every adapter integration lands first. The current iteration loop is:

1. Stop the server.
2. Edit a CLI flag, env var, or import line.
3. Restart — MiniCPM-o cold-loads in 60–120 s on b200 (`docs/model-stack.md`,
   `companion_harness/minicpm_streaming.py`).
4. Reconnect the console.
5. Replay the scenario.

Four specific pains this design targets:

- **Restart-loop friction.** A 90 s MiniCPM cold load on every config change
  dominates wall-clock when you are debugging a single adapter. For the 12
  CPU-light "hot" seams (Silero VAD, faster-whisper ASR, Kokoro TTS, scene-change
  scorer, grounding, attachment-risk monitor, …) the restart is pure overhead.
- **Adapter-first discipline test.** `CLAUDE.md` rule: "Any new model or
  library goes behind an interface in `companion_harness/`." We assert this on
  paper today; a runtime swap is the empirical check. If a seam can't actually
  be swapped, the Protocol is fictional.
- **Bug-surfacing for hidden coupling.** PR #268 (`--minicpm-only`) surfaced
  exactly this class of bug: `init_tts=True` is only payable at MiniCPM load
  time, so a flag that *looks* TTS-orthogonal silently increased VRAM and
  cold-start by ~8 s. A model-picker that exposes seams explicitly forces these
  couplings out of import order and into the dependency graph.
- **Operator iteration speed.** A/B comparing two adapter implementations
  ("Kokoro vs MiniCPM-native TTS for the same utterance") requires identical
  session state — currently impossible because each restart resets the event
  log, the wake/sleep state, and any in-flight memory items.

The longer-term framing: the manual-test console is becoming a small ablation
rig. Today it exposes 12 threshold knobs (Tier-B, see
`manual_test_console/config_schema.py:47`); the next axis is "which
implementation of seam S is wired right now."

---

## Hot vs cold seam taxonomy

A *seam* is one `Protocol`/factory boundary in `build_live_pipeline()`
(`manual_test_console/live_pipeline.py:438`). A *hot* seam is one whose adapter
can be released and rebuilt without freeing GPU memory tied to a foreground
model. A *cold* seam is one whose lifecycle is coupled to the MiniCPM-o
streaming foreground.

### Hot seams (Phase 1 target — 12 seams)

| # | Seam | Production adapter | Disabled-mode fallback (`_Null*`, not a separate toggle) | Per-seam swap cost | Live-swappable | Why |
|---|------|---------------------|----------------------------------------------------------|--------------------|----------------|-----|
| 1 | VAD | `SileroVADModel` | `EnergyVADModel` | <50 ms | Y | Pure CPU model, ~1.5 MB weights, reload trivial. |
| 2 | Smart-turn | `PipecatSmartTurnModel` | `SilenceSmartTurnModel` | <100 ms | Y | Pipecat v3 model, CPU. |
| 3 | Backchannel classifier | `ASRLexiconBackchannelModel` | `ZeroBackchannelModel` | <50 ms | Y | Lexicon table, no weights. |
| 4 | ASR | `FasterWhisperASRModel` | (None — orchestrator produces silence) | ~2 s (model warm) | Y | faster-whisper supports `del model; gc.collect()`; rebind one factory. |
| 5 | TTS | `KokoroTtsAdapter` | `NoopTtsAdapter` | ~3 s | Y | Kokoro model load is bounded; orchestrator already accepts swap (PR #181 added `--tts-adapter` selector). |
| 6 | Scene-change scorer | `CLIPSceneChangeScorer` | (None — orchestrator returns `0.0`) | ~1 s | Y | CLIP image-only branch, ~150 MB, isolated. |
| 7 | Visual grounding | `GroundingDINOAdapter` | `_NullGroundingDINOAdapter` | ~2 s | Y | Loaded behind `--enable-vision` (PR #160), already opt-in. |
| 8 | AV-conflict scorer | `HeuristicAVConflictScorer` | `_NullAudioVisualConflictScorer` | <10 ms | Y | Pure heuristic (PR #173). |
| 9 | Urgency scorer | `ProsodyLexiconUrgencyScorer` | `_NullUrgencyScorer` | <10 ms | Y | Pure heuristic (PR #180). |
| 10 | Embedder | `SentenceTransformerEmbedder` | `_NullEmbeddingAdapter` | ~1 s | Y | sentence-transformers MiniLM, ~80 MB (PR #179). |
| 11 | Attachment-risk monitor | `EventStreamAttachmentRiskMonitor` | `_NullAttachmentRiskMonitor` | <10 ms | Y | Event-stream reducer (PR #177). |
| 12 | Fast tool dispatcher (optional) | `FastToolDispatcher` | (None — routing table absent) | <50 ms | Y | Pure routing table. |

### Cold seams (Phase 3 target — 5 seams, all MiniCPM-bound)

| # | Seam | Production adapter | Why cold |
|---|------|---------------------|----------|
| 1 | Foreground model | `MiniCPMStreamingModel` | ~14 GB VRAM, ~60–120 s cold load; tearing it down mid-session strands queued audio frames. |
| 2 | Native-duplex EOU | `MiniCPMNativeDuplexEouSource` (via `native_duplex_eou` route, PR #184) | Reads MiniCPM's internal state; lifetime piggybacks on (1). |
| 3 | Addressing classifier (primary) | `MiniCPMAddressingClassifierImpl` (PR #182) | Same MiniCPM session as (1); safety-net is `WakeWordAddressingClassifier` (hot). |
| 4 | Deictic detector | `MiniCPMDeicticDetector` (via `DeicticDetector(model=...)`, PR commits 6f90e1b) | Reuses MiniCPM vision branch. |
| 5 | Provenance computer | `MiniCPMProvenanceComputer` | Same. |
| 6 | Native TTS (when selected) | `MiniCPMNativeTtsAdapter` (`--tts-adapter native_minicpm`, PR #181) | Borrows the MiniCPM decoder; swap requires foreground reload. |

> Hot seams are independent factories; cold seams form a coupling graph rooted
> at the foreground. Anchor 5 enforces this.

---

## Anchor decisions — non-transient (locked)

These five anchors are not OQs. The plan-critic round may add caveats and
gates, but the *shape* below is committed.

### Anchor 1 — Two-tier UI: Hot panel + Cold panel

The dashboard drawer (currently three sections per
`docs/manual-test-handbook.md` §2.8.2) grows a fourth section split in two:

- **Hot panel.** Each row = one of the 12 hot seams. Action = instant swap via
  `POST /config/model-swap`. UI reflects new state in <100 ms (see numeric
  gate). No restart prompt.
- **Cold panel.** Each row = one of the 5 cold seams. Action = "queue swap;
  restart required" with a visible "Apply on restart" CTA. The server kills
  itself and restarts, consuming the queued config on boot.

The split is *UI-only*. At the API layer, both produce the same event family
(`model_swap_requested` → `model_swap_completed` or `model_restart_queued` →
`model_restart_applied`). This keeps the audit shape uniform; the dashboard
just renders two affordances.

### Anchor 2 — Per-seam 2-state toggle for Phase 1: `real / disabled`

- `real`: the production adapter (e.g., `CLIPSceneChangeScorer`).
- `disabled`: factory returns None; the orchestrator's existing None-handling produces neutral behavior (per the live-pipeline opt-in pattern shipped in PR #255).

**Removed:** the `stub` middle state. Rationale: `_Null*` stubs exist for adapter-protocol fallback when a real impl is unavailable; they are not a useful operator toggle. If the operator wants neutral behavior, `disabled` already achieves it via the orchestrator's None-handling. The third state added UI complexity without operator value.

**Implication:** simpler API (`POST /config/model-swap` body becomes `{seam, enabled: bool}`), simpler UI (toggle, not 3-way selector), fewer state transitions to test.

### Anchor 3 — New event types

Four new event types, all carrying `caused_by=[operator_action.event_id]` per
the existing pattern (issue #151, currently realized by
`_make_config_change_event` at `manual_test_console/server.py:279`):

- `model_swap_requested` — operator clicked the radio. `payload =
  {seam, from_adapter, to_adapter, requested_at_ms}`.
- `model_swap_completed` — hot swap succeeded. `payload = {seam, from_state: "real"|"disabled",
  to_state: "real"|"disabled", applied_at_ms, latency_ms}`. This is the canonical event the
  replay layer consumes (see Anchor 4).
- `model_swap_rejected` — failed (e.g. dependency missing, GPU OOM, factory
  raised). `payload = {seam, attempted, reason, error_class}`.
- `model_restart_queued` — cold-seam swap pending restart. `payload = {seam,
  to_adapter, queued_at_ms}`.
- `model_restart_applied` — server restart consumed the queued config. Emitted
  on the *new* process, with `caused_by` referencing the
  `model_restart_queued.event_id` from the previous process (cross-process
  causation must survive the event-log file boundary; see OQ-7).

All five share the `config_change_30d` retention bucket (existing pattern,
`manual_test_console/server.py:274`).

### Anchor 4 — Tier-D config category proposal

The existing taxonomy is:

- **Tier A** — spec-frozen, immutable at runtime (`_TIER_A_KEYS` in
  `manual_test_console/config_schema.py:186`).
- **Tier B** — runtime-tunable threshold knobs, replay-safe (12 keys in
  `ALLOWLIST`).
- **Tier C** — startup-only knobs (CLI flags, env vars).

Model identity does not fit any of these. It is replay-*affecting* (the same
input produces a different output if you swap the ASR), but it is also
*runtime-mutable*. We propose a fourth tier:

- **Tier D** — model identity. Mutable at runtime; not replay-safe in the
  "bit-identical decision" sense (invariant #5), but replay-*reconstructable*:
  every state change is recorded as a `model_swap_completed` event in the
  causal log, and the replay layer rebuilds the per-frame active-adapter map
  by folding those events forward in time.

Tier D is what licenses the dashboard to mutate the running adapter set
without violating invariant #5. The compatibility argument: Tier-B replay
determinism is preserved *within* a contiguous span between two adjacent
`model_swap_completed` events. The replay report (`scripts/v0_1h_replay_report.py`)
gains a per-span breakdown.

Alternative considered, deferred to OQ-1: extend Tier B with model-identity
entries plus an explicit `replay_tier` marker per key. Same end state, different
schema route.

### Anchor 5 — Coupled-seam invalidation graph

The cold-seam table above is a DAG rooted at the foreground:

```
MiniCPMStreamingModel (foreground)
├── native_duplex_eou_source
├── MiniCPMAddressingClassifierImpl
├── MiniCPMDeicticDetector
├── MiniCPMProvenanceComputer
└── MiniCPMNativeTtsAdapter  (only when --tts-adapter=native_minicpm)
```

When the operator queues a foreground swap, the dashboard MUST visualize this
fan-out and require explicit acknowledgement: "Swapping the foreground will
also rebuild: native_duplex_eou, addressing (primary), deictic, provenance,
native TTS (currently selected). Continue?"

The server enforces the same invariant: a `POST /config/model-swap` against a
parent seam without a matching downstream payload is rejected with HTTP 409
and a `model_swap_rejected` event citing `reason=coupling_violation`.

The graph is exposed at `GET /config/dependencies` so the UI is data-driven
(no hand-coded fan-out per seam).

---

## Open questions (with leans)

OQ-1 through OQ-7 are intentionally underspecified. Plan-critic should pick
the ones it has the strongest opinion on.

**OQ-1 — Tier-D vs Tier-B-with-marker?**
Should model identity live in a new Tier D, or in an extended Tier B with a
per-key `replay_tier` marker?
*Lean:* new Tier D. Reasoning: the tier label communicates the replay-affecting
nature at the schema level, and existing Tier-B tooling
(`validate_patch` in `config_schema.py:218`) does not need a new dimension.

**OQ-2 — Hot-swap atomicity boundary.**
Should a new adapter activate mid-frame (interrupting the current orchestrator
tick) or only on the next frame boundary?
*Lean:* next frame boundary. Mid-frame swap risks splitting a single signal
(e.g. one half of an EOU detection comes from VAD-v1, the other from VAD-v2);
boundary swap is monotonically simpler to replay.

**OQ-3 — Dashboard state persistence across restart.**
Does the dashboard's selected adapter set survive a server restart?
*Lean:* NO at Phase 1 (operator re-applies on each boot — same UX as the
threshold panel which already resets per
`docs/manual-test-handbook.md` §2.8.5); YES at Phase 3, but only via the
queued-config mechanism (operator explicitly clicks "Apply on restart"; the
queued state lives in a single JSON file consumed once on boot, then cleared).

**OQ-4 — Multi-implementation support (`real_v2`, `real_v3`).**
Should the picker support more than one `real` impl per seam from day one?
*Lean:* defer to Phase 2 when a second real impl actually exists per seam
(today only TTS has two: Kokoro vs MiniCPM-native). YAGNI on the others.

**OQ-5 — CLI parity.**
Should every dashboard control have a `--config` JSON equivalent so operators
can script repro?
*Lean:* YES. Operators already drive the test rig from shell scripts; a
dashboard-only control would force them back into a browser for repro. The
JSON file format is the same one consumed by the queued-restart mechanism
(Anchor 1 + Phase 3), so we get this almost free.

**OQ-6 — Dependency-invalidation UX.**
When the operator queues a foreground swap, do we show a modal confirmation
listing the downstream rebuilds, or auto-reset coupled seams silently with a
post-hoc toast?
*Lean:* modal. Silent reset hides the very coupling we are trying to
surface (the original motivation, "bug-surfacing for hidden coupling," in
"Why this is needed").

**OQ-7 — Cross-process replay determinism.**
Does Tier-B replay still hold when the log contains `model_swap_completed` and
`model_restart_applied` events from a different process?
*Lean:* yes, provided the replay layer treats every `model_swap_completed` as
a fold-left state transition over the adapter map, and treats
`model_restart_applied` as a logical no-op for replay (the new process's
adapter set is reconstructed from the cumulative event sequence, not from the
restart event itself). The `caused_by` chain crosses the file boundary
because we cite the previous process's `model_restart_queued.event_id`.

---

## Phased rollout

Three phases. Each phase ships as its own `plan-*-execution.md` and its own
PR. No phase depends on a phase later than itself.

### Phase 1 — Hot swap, 2-state toggle (small PR, ~6–8 tasks)

**Scope:** the 12 hot seams. Real / disabled toggle. New
`/config/model-swap` route. Four new event types (`model_swap_requested`,
`model_swap_completed`, `model_swap_rejected`, plus the operator_action root
already produced by `_handle_post_config_patch` at
`manual_test_console/server.py:600`). UI extension in the existing tuning
drawer.

**Tasks (preview, not authoritative):**

1. Adapter registry module (mirrors `companion_harness/evals/registry.py` from
   `plan-eval-console-pr1-draft.md` Anchor 2): per-seam `AdapterInfo` with
   `seam`, `name`, `status`, `factory`, `dependencies`.
2. `GET /config/seams` returning current per-seam state.
3. `POST /config/model-swap` happy-path (hot seam only).
4. New event types + retention wiring (reuse `config_change_30d`).
5. UI: extend the tuning drawer with the hot panel; per-seam toggle (real/disabled).
6. Contract test: every dashboard swap emits exactly one
   `model_swap_completed` (audit-completeness gate = 1.0).
7. Contract test: replay determinism within a span between two
   `model_swap_completed` events (extend the `v0_1h_replay_report.py`
   per-span breakdown stub).
8. Handbook §2.8 extension documenting the hot panel.

### Phase 2 — Multi-impl picker for hot seams (medium PR, ~4–5 tasks)

**Scope:** dropdown replacement for seams with ≥2 real impls. Today this is
TTS only (Kokoro vs MiniCPM-native, already selectable via `--tts-adapter` per
PR #181). Plumbing for VAD alternates and ASR alternates lands here even
though no second impl exists yet, so the registry shape is stable.

**Tasks (preview):**

1. Extend `AdapterInfo` to list multiple `real` impls per seam.
2. UI: replace radio with dropdown for multi-impl seams.
3. Cross-impl contract test: swapping between two real impls of the same
   seam preserves orchestrator wiring (no factory raises).
4. Handbook update.

### Phase 3 — Cold-seam queued restart (medium PR, ~6–8 tasks)

**Scope:** cold panel UI; queued-config JSON file; server restart consumes
queued config on boot; cross-process `caused_by` chain in event log;
dependency-graph UI rendering `GET /config/dependencies`.

**Tasks (preview):**

1. `GET /config/dependencies` route + the static graph definition.
2. Queued-config file format + atomic write.
3. `POST /config/restart` handler (emits `model_restart_queued`; triggers
   process exit via `os._exit` or systemd-style supervisor signal).
4. Boot path: read queued config, emit `model_restart_applied` citing the
   previous queued event_id, apply, delete queued file.
5. UI: cold panel + modal confirmation (per OQ-6 lean).
6. Contract test: cross-process `caused_by` chain survives a restart.
7. Contract test: replay determinism across a `model_restart_applied` event
   boundary.
8. Handbook §2.8 extension documenting the cold panel and restart flow.

---

## API surface

All new routes under `/config/*` to keep the existing route family intact
(`manual_test_console/server.py:856` is the current registration site).

| Route | Method | Body / params | Returns | Phase |
|-------|--------|---------------|---------|-------|
| `/config/seams` | GET | — | `{seams: [{seam, status, current_adapter, available_adapters}]}` | 1 |
| `/config/model-swap` | POST | `{seam, enabled: bool, adapter_id?}` | `{accepted, model_swap_event_id, restart_required: bool}` | 1 |
| `/config/dependencies` | GET | — | `{nodes: [...], edges: [{from, to, kind}]}` | 3 |
| `/config/restart` | POST | `{confirm: true}` | `{accepted, model_restart_queued_event_id}` then process exit | 3 |

Existing routes (`GET /config`, `POST /config/patch`, `POST /config/reset`) are
untouched. Tier-B and Tier-D coexist; a single `/config` GET response gains a
`seams` block in Phase 1.

---

## UI sketch

The right-column tuning drawer (already established by the 12 threshold
sliders, `docs/manual-test-handbook.md` §2.8) grows two new sections above
the existing three:

```
┌─ Tuning ──────────────────────────────────┐
│ ▼ Hot seams (12)                          │
│   VAD              [● on  ○ off]          │
│   Smart-turn       [● on  ○ off]          │
│   Backchannel      [● on  ○ off]          │
│   ASR              [● on  ○ off]          │
│   TTS              [Kokoro      ▾]        │  ← Phase 2 dropdown
│   Scene change     [● on  ○ off]          │
│   Grounding        [○ on  ● off]          │
│   AV conflict      [● on  ○ off]          │
│   Urgency          [● on  ○ off]          │
│   Embedder         [● on  ○ off]          │
│   Attachment risk  [● on  ○ off]          │
│   Fast tool disp.  [○ on  ● off]          │
│                                           │
│ ▼ Cold seams (5)  — restart required      │
│   Foreground       [● MiniCPM-o (cur) ▾]  │
│   Native EOU       [● coupled to fg]      │
│   Addressing (1°)  [● coupled to fg]      │
│   Deictic          [● coupled to fg]      │
│   Provenance       [● coupled to fg]      │
│        [ Apply on restart ]               │
│                                           │
│ ▼ Policy thresholds (3)  (existing)       │
│ ▼ Detectors (5)          (existing)       │
│ ▼ Orchestrator (4)       (existing)       │
└───────────────────────────────────────────┘
```

The audit-tail click-to-jump behavior (`docs/manual-test-handbook.md` §2.8.4)
extends to `model_swap_completed` events.

---

## Numeric gates

These are *acceptance gates* for the phased PRs, not soft targets. Phrased to
mirror the existing gate style in `architecture-v0.1.md` Part 8 and the
roadmap docs.

| Gate | Threshold | Why this number |
|------|-----------|-----------------|
| `model_swap_audit_completeness` | == 1.0 | Every dashboard swap MUST produce exactly one `model_swap_completed` (or `_rejected`) event. Anchor 3 contract — invariant #1 ("no unlogged behavior"). |
| `hot_swap_latency_ms_p95` | < 100 | Operator-perceived snappy. Above 100 ms the picker stops feeling instant; below it, the UI does not need a spinner. Excludes initial weight load for cold-started seams (e.g. first Kokoro swap of the session). |
| `replay_determinism_after_swap` | == 1.0 | Tier-B replay (invariant #5) MUST hold within any span bounded by `model_swap_completed` events. This is the licensing argument for Tier D. |
| `cold_restart_caused_by_continuity` | == 1.0 | Phase 3 only: every `model_restart_applied` MUST cite a `model_restart_queued.event_id` from the prior process. Invariant: the DAG closes across the process boundary. |

Each gate maps to one Phase-1 or Phase-3 contract test in the task lists
above.

---

## Risks

- **Tier-D taxonomy expansion may cascade.** Once "model identity" is a tier,
  the next request will be "log-level identity" or "feature-flag identity."
  Mitigation: the tier label is descriptive of *replay semantics*, not
  *operational origin*; new categories must justify their replay-tier
  classification.
- **Mid-flight signal split on hot swap.** A swap that lands between two
  frames of the same EOU detection could split the signal across two adapters
  and produce a non-replayable artifact. Mitigated by OQ-2 lean (next-frame
  boundary) plus a contract test that injects a swap during a multi-frame
  detection.
- **Cold-restart queue mismanagement.** Operator queues a swap and forgets to
  click "Apply on restart"; the queued JSON file persists and surprises the
  *next* boot. Mitigation: queued config has a TTL (default 1 h) and the boot
  path warns visibly if the queued file is >1 minute old.
- **Coupling-graph errors silently invalidate Tier-B replay.** If the
  dependency graph (Anchor 5) is wrong — e.g. a derivative is omitted — a
  parent swap leaves a stale child wired to a dead model. Mitigation: the
  graph is exposed via `GET /config/dependencies` so the replay-layer
  contract test can read the same source of truth.
- **VRAM thrash on repeated foreground swaps.** Phase 3 hazard: queuing and
  applying foreground swaps rapidly could exhaust b200 VRAM if cleanup is
  incomplete. Mitigation: cold-seam swap requires a full process restart by
  design (not a hot tear-down); the OS reaps GPU memory.

---

## Cross-references

- `docs/architecture-v0.1.md` Part 2 (invariants #1, #5, #9) — the replay-
  determinism license for Tier D.
- `docs/manual-test-handbook.md` §2.8 — existing tuning-drawer semantics that
  the new picker extends; §2.8.4 audit-tail click-to-jump pattern reused for
  `model_swap_completed`.
- `docs/model-stack.md` — current adapter inventory; the model-picker UI
  literally renders this list.
- `manual_test_console/config_schema.py` — current Tier-B `ALLOWLIST` (12
  keys) + `_TIER_A_KEYS`; Tier D extends this taxonomy.
- `manual_test_console/server.py:570–760` — existing `/config/*` route family
  + `_make_operator_action_event` / `_make_config_change_event` patterns that
  Anchor 3 reuses verbatim.
- `manual_test_console/live_pipeline.py:438` — `build_live_pipeline()`
  factory injection sites that become per-seam swappable.
- `plan-eval-console-pr1-draft.md` Anchor 2 — adapter-registry pattern
  precedent (`companion_harness/evals/registry.py`).
- PR #160 — `--enable-vision` opt-in pattern; the `disabled` state in Anchor 2
  reuses this.
- PR #181 — `--tts-adapter` selector; Phase 2 dropdown is its dashboard
  surfacing.
- PR #268 — `--minicpm-only` flag; the `init_tts=True` coupling bug is the
  named motivating example in "Why this is needed."

### Hidden-coupling example (real motivation)

During the manual test that produced PR #268, adding `--minicpm-only`
unexpectedly increased MiniCPM cold-load by ~8 s. Root cause: the
foreground-load path passes `init_tts=True` by default, and `--minicpm-only`
did not override it, so MiniCPM was loading its native TTS branch even though
the configured `TtsAdapter` was Kokoro (or noop). The coupling was invisible
at CLI level. A dashboard model-picker that shows `[Foreground: MiniCPM (TTS:
on)]` as one row, and `[TTS adapter: Kokoro]` as another, would have made
this conflict diagnosable at swap time instead of at load time.

---

## Out of scope

- **Hot-swap of cold seams.** Cold seams require a full process restart by
  design (Anchor 1 split). Anyone proposing in-place foreground swap should
  open a separate plan doc with a VRAM-safety argument.
- **Multi-user / concurrent config.** Manual-test console is single-operator
  by construction. No locking, no last-writer-wins, no per-session config
  scoping.
- **Persistent config across restarts (general case).** Phase 3 ships
  *queued* config (one-shot, consumed at next boot). General persistence
  (every swap survives every restart) is a separate design.
- **New real model implementations.** This plan is wiring + UX only. The
  registry's `real` slot enumerates adapters that *already exist* in
  `companion_harness/`. Adding a second real ASR or a second real VAD is
  out of scope (and is what Phase 2's multi-impl picker exists to surface
  *once they exist*).
- **Telemetry / dashboards for swap frequency.** A "which seams get swapped
  most" report is a downstream eval-subsystem concern, not a console concern.

---

## Convergence notes (for plan-critic)

When this draft enters `/plan-review`, the highest-leverage critique surfaces
are likely:

1. Is Tier D actually a new tier, or is it Tier B with a marker? (OQ-1.)
2. Does Phase 1's `replay_determinism_after_swap == 1.0` gate over-promise
   given that `v0_1h_replay_report.py` is still tolerance-based per
   invariant #6?
3. Is the cold-seam DAG (Anchor 5) complete? Specifically: does
   `MemoryWriter` or any v0.1h memory-wiring adapter (PR #165) need to be
   on it?
4. Should Phase 1 also surface `--minicpm-only` style aggregate presets, or
   is per-seam enough?
5. Are the new event types subject to bi-temporal `valid_from/valid_to`
   semantics (per invariant #3), or are they pure operator-action audit
   events outside the memory subsystem?

These are pre-empted here so plan-critic can either accept the leans, push
back, or add new OQs.
