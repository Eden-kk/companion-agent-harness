# Roadmap — v0.2 (DRAFT)

## Status: **DRAFT** — 2026-05-15

> v0.2 scope is **production-quality, not skeleton-completion**. v0.1a → v0.1j shipped the spec's full Stage 0–6 stack: every signal seam, every adapter Protocol, every policy path, every gate, the dashboard, the replayer, the 4-store memory, the 8-rubric, the attachment-risk monitor, v0.1j locked the no-unmarked-stub discipline (real producer populated OR deterministic default stub tagged `# UNAVAILABLE:`). Real-adapter implementations + live-pipeline wiring for the 8 seams shipped across PRs #234-#266 and the post-Round-4 sweep (see `docs/model-stack.md`). The pinned v0.1 success criterion ("explain every utterance, replay every policy decision, stop when interrupted, wait through thinking pauses, answer direct questions promptly") is met.
>
> v0.2 takes the harness from *demonstrable* to *host-ready*. Each task is a capability-upgrade to an existing v0.1 stage, replacing a placeholder or mechanical fallback with a production-quality backend. No new spec stages are introduced. The spec remains FROZEN.
>
> The defining design discipline of this milestone is **invariant #6** (behavioral tolerance for end-to-end replay): v0.2 expands the real-model surface, which means more sources of non-determinism on the foreground/proposal path. The Tier-B policy path stays bit-identical; the Tier-A behavioral path tolerates the new variance via the `(same_action_class, same_timing_bucket ±200ms, same_interaction_intent, same_safety_class)` tuple.

## v0.2 pinned success criterion

> **v0.2 succeeds when the harness is host-ready — real reasoning, real attribution, real benchmarks, real persistence — without operator workarounds. (Production deployment posture is deferred to v0.3 per Anchor 5.) Specifically:**
> 1. The smart-path tool router (`BackgroundReasoner`) ships **MCP-primary and LLM-direct backends behind the `BACKGROUND_REASONER` flag**. Default backend remains `fake` until rollout gates (`background_reasoner_budget_exhaustion_rate < 0.05` for 24 hours of production use) pass. Default flip is a v0.2-final task, not a v0.2 entry gate.
> 2. The addressing classifier consumes real speaker-diarization output, not the mechanical `solo` social-mode fallback.
> 3. Eval Phase C (live examiner) ships both synthetic-mode framework AND a small real-diarized fixture pack (3+ multi-speaker recordings); real-mode against live operator recordings beyond this fixture pack is deferred to v0.3.
> 4. Real CANDOR and FullDuplexBench data flow through the existing `CaseSource` Protocols without `NotImplementedError`.
> 5. The `forget that` command persists a bi-temporal tombstone visible in `retrieve()` queries (shipped via PR #253 — backfilled into v0.2 scope).
> 6. The 6 opt-in real adapters from the post-v0.1j stub-replacement sweep (CLIP, GroundingDINO, AV-conflict, MiniCPM-deictic, prosody-urgency, sentence-transformer) have an explicit default-on / default-off decision recorded.
> 7. The harness is **host-ready outside the manual-test rig** — runtime hostability capabilities only (replay-report export, durable session-blob storage, `/healthz` extensions for external monitors). **Production deployment posture** (Docker, systemd, k8s, secrets, log shipping) remains explicitly deferred to v0.3.
> 8. POLICY_VERSION reaches `v0.2-final` via cadence `v0.1j → v0.1k (at diarization land) → v0.2-final (at milestone tag)`.

## Anchor decisions — non-transient (locked)

Most v0.2 design choices are reversible behind existing Protocol seams. The five anchors below are the choices that bake into stored data, replay surfaces, or external integrations and are NOT cheap to reverse.

### Anchor 1 — POLICY_VERSION cadence: two-step bump (`v0.1k` then `v0.2-final`)

`v0.1k` lands when the diarization wiring (#8) adds `current_speaker_id` to `PolicyInputs` and the speaker-continuity tie-breaker becomes a replay-affecting rule. `v0.2-final` is the milestone-tag bump.

**Justification:** the only replay-affecting policy change in v0.2 scope is diarization-driven addressing. Bundling it into a single `v0.2-final` bump would force every other v0.2 PR to wait for diarization. Two-step cadence lets non-policy work ship continuously.

**Replay impact:** every fixture pinning `policy_version` updates at each bump. `test_policy_replay_exact` extends with both new versions.

### Anchor 2 — MCP-primary background reasoner with LLM-direct fallback

`BACKGROUND_REASONER={fake,mcp,llm}` environment variable + `--background-reasoner` CLI flag. Default remains `fake` until at least one MCP server is wired in production. The `fake` mode preserves all v0.1f contract tests.

**Justification:** MCP is the spec-aligned tool-routing protocol; LLM-direct is a pragmatic fallback when MCP infrastructure is not yet hosted. Both backends emit the identical `tool_call_*` event chain so the orchestrator wiring is unchanged.

**Rejected:** custom orchestration framework (LangChain, LlamaIndex). MCP is the spec-aligned choice. See `plan-real-background-reasoner-draft.md` for details.

### Anchor 3 — Diarization adapter is opt-in via `--enable-diarization`; defaults OFF

Same posture as the 6 v0.1j real adapters: default OFF preserves backward compat. Operators opt in when their session warrants it (multi-speaker, longer recording, eval Phase C run).

**Justification:** diarization adds ~50ms p95 latency per chunk + ~30MB resident memory. Single-operator manual tests don't need it. Eval Phase C runs require it.

**Replay impact:** when `--enable-diarization` is OFF, `current_speaker_id=None` flows through PolicyInputs and the v0.1k tie-breaker is a no-op. Bit-identical Tier-B replay vs v0.1j fixtures is preserved.

### Anchor 4 — Real-mode benchmark loaders require explicit `--mode real` opt-in

CLI flag on `python -m companion_harness.evals run`. Default `synthetic`. Real-mode runs may take hours; surprising default behavior is operator-hostile.

**Justification:** synthetic-mode is the fast feedback loop (CI, iteration). Real-mode is the periodic regression-detection loop (operator-scheduled, longer).

**Dataset version pinning:** every adapter pins `revision=` in `load_dataset()` to a specific commit hash. Bit-identical real-mode scores across runs are gated on the pin.

### Anchor 5 — Production deployability is a separate scope, not absorbed into v0.2

v0.2 does NOT ship a production deployment posture (Docker, systemd, k8s, secrets management, log shipping). v0.2 ships the runtime *capabilities* needed for production; v0.3 will ship the *infrastructure*.

**Justification:** the harness's production posture depends on the deployment target (single-host, multi-host, edge, cloud) which is a separate operator decision. Conflating it with capability work creates scope creep.

**What v0.2 DOES ship for deployability:** replay-report export hardening, durable session-blob retention, blob-store rotation, and `/healthz` extensions for external monitors.

## Closed decisions (OQs 1–8, all RESOLVED 2026-05-15)

- **OQ-1**: Bundle v0.2 as a single milestone? **YES** — coherent theme (production quality), shared anchor decisions, two-step POLICY_VERSION cadence is natural. But each task ships as its own PR; the milestone is a banner, not a bundled merge.
- **OQ-2**: Include Eval Phase C in v0.2 scope? **YES** — gated on diarization (#8) anyway, completes the eval-subsystem story. Live examiner becomes exercisable.
- **OQ-3**: Bump POLICY_VERSION once or twice? **TWICE** (Anchor 1) — let non-policy PRs ship continuously.
- **OQ-4**: Default opt-in for the 6 v0.1j real adapters? **DEFER per-adapter** to v0.2 Task 16 (review acceptance criteria per adapter; flip defaults only where the adapter has proven low-risk on hardware budget AND high signal value).
- **OQ-5**: Tombstone for `forget that` in v0.2 scope? **YES** — already shipped via PR #253 on 2026-05-15; backfill into v0.2 scope as Task 0.
- **OQ-6**: Real BackgroundReasoner backend choice — MCP or LLM-direct? **BOTH** (Anchor 2) — MCP primary, LLM fallback, fake preserved.
- **OQ-7**: Production deployment in v0.2? **NO** (Anchor 5) — separate v0.3 scope.
- **OQ-8**: Spec amendment in v0.2? **NO** — spec remains FROZEN. All v0.2 work is capability-upgrades to existing stages.

## Tasks (20 implementation tasks + 2 backfilled (Wave 0) = 22 total items across 7 waves)

> Wave 0 below is **already shipped** — not a release wave. The 20 implementation tasks live in Waves 1–6, plus the prerequisite Wave 0′ (Eval Phase A.5) detailed below.

### Wave 0 — Already shipped (backfilled into v0.2 scope)

- **Task 0a (shipped).** `forget that` → bi-temporal tombstone via SleepTimeAgent. PR #253.
- **Task 0b (shipped, PR #255 merged).** 6 real-adapter opt-in CLI flags for the live pipeline.

### Wave 0′ — Prerequisite: Eval Phase A.5 (must complete before Wave 1+)

Per GPT's deep-research catch: ROADMAP.md lists Eval Phase A.5 as active work, and v0.2 Wave 4 (live examiner) depends on its deterministic replay-safe fixtures. Phase A.5 must complete (or be formally absorbed into v0.2 scope as Task 0c) before Waves 1–6 commence.

- **Task 0c (prerequisite).** Complete Eval Phase A.5: `SyntheticClock`, `DirectAudioInputFeeder`, `FixtureScenarioDriver`, replay-safe contract coverage. Success: `test_eval_run_replay_safe` no longer xfailed; per-case deterministic replay is part of normal CI/local testing.

### Wave 1 — Real BackgroundReasoner (smart-path tool router)

Per `plan-real-background-reasoner-draft.md`:

- **Task 1.** `MCPClient` Protocol + concrete adapter using `mcp` Python SDK.
- **Task 2.** `MCPBackgroundReasoner(BackgroundReasoner)` implementing `select_and_call` via MCP tool routing.
- **Task 3.** `LLMBackgroundReasoner(BackgroundReasoner)` implementing the same Protocol via MiniCPM-o.chat() with structured tool-call prompt.
- **Task 4.** Budget enforcement (wall-clock + step count) on the `BackgroundReasoner` base class. `BackgroundReasonerBudgetExhausted` exception.
- **Task 5.** ConfigStore Tier-B knobs: `reasoner.budget_wall_clock_s`, `reasoner.budget_step_count`.
- **Task 6.** `BACKGROUND_REASONER` env + `--background-reasoner` CLI flag dispatcher in `manual_test_console/server.py`.

### Wave 2 — Real diarization adapter (POLICY_VERSION bump path)

Per `plan-real-diarization-adapter-draft.md`:

- **Task 7.** `DiarizationAdapter` Protocol + `_NullDiarizationAdapter` (returns `(None, 0.0, False)`).
- **Task 8.** `PyannoteDiarizationAdapter` — streaming `pyannote/speaker-diarization-3.1` with per-session embedding registry + agent-TTS mute-window.
- **Task 9 (POLICY_VERSION bump v0.1j → v0.1k).** Add `current_speaker_id: str | None` to `PolicyInputs`. Extend `SpeakPolicy.decide()` with speaker-continuity tie-breaker. Update all `policy_version`-pinning fixtures. POLICY_VERSION literal bumped at this task.
- **Task 10.** Extend `MiniCPMAddressingClassifier` to consume `current_speaker_id` in its yes/no prompt.
- **Task 11.** `--enable-diarization` CLI flag wiring in `manual_test_console/server.py`.

### Wave 3 — Real benchmark data loaders (real-mode CANDOR + FDB)

Per `plan-real-benchmark-data-loaders-draft.md`:

- **Task 12.** HF `datasets` streaming loaders for CANDOR (V1) — `_load_candor_streaming` + `_candor_row_to_evaluation_case` mapper.
- **Task 13.** HF `datasets` streaming loaders for FullDuplexBench V1 + V1.5.
- **Task 14.** `--mode {synthetic,real}` + `--limit N` + `--filter K=V` CLI flags on `python -m companion_harness.evals run`.

#### Dataset governance (applies to Tasks 12–14)

- **HuggingFace auth.** Each dataset's gate posture is documented per loader; required tokens (`HF_TOKEN` env var) are checked at loader init and produce an actionable error if missing.
- **License pinning.** Per-dataset license is pinned in the loader docstring. Accepted licenses: research-only (CANDOR, gated FDB splits) AND commercial-OK (where applicable). Loaders refuse to run if the upstream license changes from the pinned value.
- **`revision=` pinning.** Every `load_dataset()` call pins `revision=` to a specific commit hash for reproducibility. Bumping the pin is a deliberate PR.
- **Local cache budget.** Per-dataset cache size budget is documented in `eval-quickstart.md`; total budget bounded so a clean checkout fits on the b200 dev volume. Retention defaults to "keep only the pinned revision."
- **Skip-row vs fail-run semantics.** Malformed rows are skipped and counted (`dataset_row_skip_rate` gate). Loader failures (auth, network, license change) fail the run loudly — never silently degrade to a partial result.
- **Smoke vs full-run expectations.** `--limit 5` smoke runs are the CI-friendly mode (seconds). Full real-mode runs (hours) are operator-scheduled and tagged in the replay-report header so they are not confused with smoke runs.

### Wave 4 — Eval Phase C (live examiner) — gated on Wave 2

- **Task 15a (framework).** `LiveExaminerCaseSource` implementing the eval case-source Protocol against a recorded session's event log. Uses diarization output to attribute utterances to specific speakers; uses Tier-B replayer to reconstruct policy decisions. Synthetic-mode only; real-mode raises `NotImplementedError`.
- **Task 15b (fixture pack).** Curated diarized-session fixture set (≥3 multi-speaker recordings, ground-truth speaker labels, replay-safe event logs). Required for Phase C to actually run in real mode. Without this fixture pack, Wave 4 is incomplete.

### Wave 5 — Default-on decision for v0.1j real adapters

- **Task 16.** Per-adapter acceptance criteria: GPU footprint < 1 GB; p95 latency contribution < 50ms; false-positive rate measurable on the harness-native eval suite. Flip `--enable-X` defaults to ON only where all three pass. PR per adapter (max 6 PRs in this wave, each tiny).

### Wave 6 — Deployability (capability-only; infra deferred to v0.3)

- **Task 17.** Replay-report export hardening (deterministic file paths, structured JSON output, blob-export tar generator).
- **Task 18.** Durable session-blob retention: opt-in `--blob-retention-days N` flag; auto-rotate beyond the window.
- **Task 19.** `/healthz` extensions for external monitors: per-adapter readiness, GPU memory budget, last-N-event-rate counters.
- **Task 20 (POLICY_VERSION bump v0.1k → v0.2-final).** Final milestone-tag bump. `git tag v0.2`. `scripts/v0_2_replay_report.py` mirroring the v0.1j script.

## Parallelizability

Waves 1, 2, 3 are independent. They can run concurrently across worktrees.

Wave 4 (Phase C) is gated on Wave 2 (diarization).
Wave 5 needs Task 0b (live-wiring landed).
Wave 6 Task 20 is the LAST task (final POLICY_VERSION bump + tag).

## Numeric gates table

Carry forward all v0.1a–v0.1j gates verbatim. v0.2 adds:

> Each gate carries `owner` (who is on the hook), `measurement_method` (how the value is derived), `sample_size` (how much data the value summarizes), and `gate_type` (`blocking` = milestone fails if missed; `informational` = recorded, not gating).

**Spec gates (production-quality, all stages):**

| Metric | Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|---|
| `background_reasoner_budget_exhaustion_rate` | < 0.05 (tolerable) | Wave 1 task owner | Count of `BackgroundReasonerBudgetExhausted` events / total `select_and_call` invocations, per replay report | ≥ 200 invocations | blocking |
| `background_reasoner_summary_set_context_attribution_rate` | == 1.0 | Wave 1 task owner | `summary_set_context` events with non-null `caused_by[]` reaching a `tool_call_completed` / total `summary_set_context` events | All emitted in run | blocking |
| `diarization_latency_ms_p95` | < 50ms | Wave 2 task owner | Per-chunk wall-clock measured at `DiarizationAdapter.process_chunk()` boundary | ≥ 1000 chunks (≥ 1 session) | blocking |
| `diarization_speaker_continuity_addressing_accuracy` | > 0.85 (vs Phase C ground truth) | Wave 2 + Wave 4 owners | Per-utterance addressing decision vs `live_examiner_diarized_session_001` ground-truth labels | All Phase C fixture utterances | blocking |
| `diarization_false_speaker_change_rate` | < 0.05 | Wave 2 task owner | Speaker-id transitions in adapter output / total chunks, on TTS-feedback fixture | All `diarization_acoustic_feedback_001` chunks | blocking |
| `tombstone_provenance_chain_closure_rate` | == 1.0 | Task 0a (already shipped, PR #253) | Every tombstoned `MemoryItem` has `valid_to` set with a timestamp ≤ now; tombstoned items are filtered from `retrieve()` queries; the original `source_event_id` (not `superseded_by`) is the audit-trail link back to the triggering `explicit_forget` event | All tombstones in test corpus | blocking |
| `real_mode_eval_score_replay_match_rate` | == 1.0 (deterministic given pinned `revision=`) | Wave 3 task owner | Two real-mode runs of same fixture, same `revision=`, exact score match | All real-mode smoke fixtures | blocking |

**Harness-derived (production-quality):**

| Metric | Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|---|
| `dataset_row_skip_rate` | < 0.05 (real-mode CANDOR / FDB) | Wave 3 task owner | Skipped rows / total rows observed, per loader, per real-mode run | Per real-mode run | blocking |
| `production_session_uptime_24h_p99` | > 0.99 (informational at v0.2; gated in v0.3) | Operator | `/healthz` poll uptime over rolling 24h window | ≥ 24h continuous run | informational |
| `policy_replay_exact_v0_2_final` | == 1.0 | Wave 6 task owner (Task 20) | `test_policy_replay_exact` across all v0.1 + v0.2 fixtures at the v0.2-final POLICY_VERSION pin | All policy-version-pinning fixtures | blocking |
| `real_backend_adoption_rate` | informational at v0.2; baseline establishment | Wave 5 owner | % of manual/eval sessions not using `_Null*` fallbacks (derived from replay-report header) | All sessions in reporting window | informational |
| `mcp_fallback_rate` | informational at v0.2 | Wave 1 task owner | % of `BackgroundReasoner.select_and_call` invocations that fell from MCP to LLM-direct | All invocations in reporting window | informational |
| `doc_sync_freshness_days` | < 14 days | Project lead | Days since README / ROADMAP / v0.2 draft were last reconciled (git-log derived) | One sample per check | informational |
| `adapter_default_on_readiness_count` | == 6 by v0.2-final (informational interim) | Wave 5 task owner | Count of opt-in adapters with complete profiling evidence (GPU footprint + p95 latency + false-positive measurement) | One sample per adapter | informational |
| `local_ci_pass_rate` | == 1.0 | Each task owner | `pytest` pass rate on the local-only contract test suite per `docs/remote-dev.md` | All contract tests | blocking |
| `remote_smoke_pass_rate` | == 1.0 | Each task owner | `pytest` pass rate on the b200 GPU smoke suite per `docs/remote-dev.md` | All b200-tagged smoke tests | blocking |

## Fixture manifest (additive to v0.1)

- `diarization_speaker_continuity_001` — two-speaker recording, addressing classifier should follow the wake-worder.
- `diarization_acoustic_feedback_001` — TTS re-captured by mic, mute-window should suppress new-speaker label.
- `reasoner_budget_wallclock_001` — long-running tool that exhausts wall-clock budget.
- `reasoner_budget_stepcount_001` — recursive tool chain that exhausts step count.
- `reasoner_mcp_unknown_tool_001` — MCP returns a tool not in local registry; expect `signal_producer_fallback`.
- `candor_real_mode_smoke_001` — 5-case CANDOR real-mode smoke (CI-friendly subset).
- `fdb_v1_real_mode_smoke_001` — 5-case FDB V1 real-mode smoke.
- `fdb_v15_real_mode_smoke_001` — 5-case FDB V1.5 real-mode smoke.
- `live_examiner_diarized_session_001` — recorded multi-speaker session for Phase C.
- `forget_tombstone_query_match_001` — already covered by PR #253's test suite; cross-listed here.

## §Coordination notes

- **POLICY_VERSION two-step**: every fixture pinning `policy_version` must update twice (at v0.1k land + at v0.2-final land). Mitigation: schema-test that policy_version is sourced from a single constant + replay-fixture migration in same PR.
- **HF datasets gating**: pyannote v3.1 + CANDOR + FDB may require HuggingFace user agreement. Document in `eval-quickstart.md` and `remote-dev.md`. Provide ungated v3.0 pyannote fallback.
- **MCP ecosystem maturity**: MCP servers are emerging; quality varies. Mitigation: lean on LLM-fallback path; gate MCP rollout on per-server reliability.
- **Default-on cascade**: flipping `--enable-X` defaults changes the manual-test baseline. Mitigation: cumulative changelog in handbook + project-progress doc, plus per-adapter PR.

## §Cross-references

- Spec: `docs/architecture-v0.1.md` (FROZEN — never edit).
- Design drafts (PR #251): `docs/plan-real-background-reasoner-draft.md`, `docs/plan-real-diarization-adapter-draft.md`, `docs/plan-real-benchmark-data-loaders-draft.md`.
- Predecessor: `docs/roadmap-v0.1j-draft.md` (final v0.1 milestone).
- Tombstone (Task 0a): PR #253 merged 2026-05-15.
- Live-wiring (Task 0b): PR #255 merged.
- Model-stack reference: `docs/model-stack.md`.
- Manual-test handbook: `docs/manual-test-handbook.md` (post-v0.1j refresh).
- Session-handoff doc: `docs/project-progress-2026-05-15.md`.

## §Risks

1. **Two-step POLICY_VERSION bump migration fatigue.** Every fixture pinning the version updates twice. Mitigation: tooling that finds all `"v0.1j"` literals and proposes the migration; manual verify per fixture.
2. **Pyannote acoustic feedback edge cases.** TTS re-captured by mic may not be cleanly suppressed by the mute-window in all room conditions. Mitigation: ground-truth recording per Wave 2 Task 8 contract test.
3. **MCP server reliability.** External tool servers may flake. Mitigation: LLM fallback + per-server reliability monitor + `signal_producer_fallback` event emission.
4. **Real-mode benchmark run-time.** Hours, not minutes. Mitigation: `--limit` flag for smoke; full runs are operator-scheduled.
5. **Default-on cascade surprise.** Flipping `--enable-clip-scene` to default-on changes resource use for every fresh `manual_test_console.server` invocation. Mitigation: per-adapter PR with explicit changelog entry; revert path is one-line.
6. **Documentation synchronization failure.** README, ROADMAP, and milestone-specific docs (e.g. `roadmap-v0.2-draft.md`) drift apart — the current PR sequence exposed README being ~10 milestones stale and ROADMAP CURRENT-MILESTONE being ~7 milestones stale. Mitigation: every milestone PR must include a README + ROADMAP update line in its description; a future CI lint compares the three docs' milestone-pointer freshness and fails the PR if they diverge.
7. **Pipeline bus-factor risk.** The current autonomous-agent shipping pipeline is a single operator + a single Claude session + the subagent / worktree infrastructure documented in `CLAUDE.md` + project memory. If any leg breaks (operator unavailable, prompt drift, undocumented agent-coordination pattern), throughput collapses. Mitigation: preserve the pipeline conventions in `CLAUDE.md` + project memory; explicitly document the agent-coordination patterns (parallel-developing, ship-feature, plan-review, pipeline-dev) so they are recoverable from the repo alone.

## §Out of scope (deferred to v0.3+)

- Production deployment posture (Docker, systemd, k8s, secrets, log shipping).
- Multi-host horizontal scaling.
- Cross-session speaker recognition (single-session at v0.2).
- Recursive reasoner-calling-reasoner.
- Custom MCP server implementation.
- Real-mode synthetic-mode score parity (numeric scores legitimately differ).
- v0.1g Task 21 RCT methodology doc (advisory; not gating; revisit at v0.3).
