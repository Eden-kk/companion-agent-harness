# Roadmap — v0.2 (DRAFT)

## Status: **DRAFT** — 2026-05-15

> v0.2 scope is **production-quality, not skeleton-completion**. v0.1a → v0.1j shipped the spec's full Stage 0–6 stack: every signal seam, every adapter Protocol, every policy path, every gate, the dashboard, the replayer, the 4-store memory, the 8-rubric, the attachment-risk monitor, all 8 stubs now have real backends. The pinned v0.1 success criterion ("explain every utterance, replay every policy decision, stop when interrupted, wait through thinking pauses, answer direct questions promptly") is met.
>
> v0.2 takes the harness from *demonstrable* to *deployable*. Each task is a capability-upgrade to an existing v0.1 stage, replacing a placeholder or mechanical fallback with a production-quality backend. No new spec stages are introduced. The spec remains FROZEN.
>
> The defining design discipline of this milestone is **invariant #6** (behavioral tolerance for end-to-end replay): v0.2 expands the real-model surface, which means more sources of non-determinism on the foreground/proposal path. The Tier-B policy path stays bit-identical; the Tier-A behavioral path tolerates the new variance via the `(same_action_class, same_timing_bucket ±200ms, same_interaction_intent, same_safety_class)` tuple.

## v0.2 pinned success criterion

> **v0.2 succeeds when the harness is deployable — real reasoning, real attribution, real benchmarks, real persistence — without operator workarounds. Specifically:**
> 1. The smart-path tool router (`BackgroundReasoner`) is backed by MCP (primary) or LLM-direct (fallback), not the v0.1f deterministic fake.
> 2. The addressing classifier consumes real speaker-diarization output, not the mechanical `solo` social-mode fallback.
> 3. Eval Phase C (live examiner) is exercisable against real diarized recordings.
> 4. Real CANDOR and FullDuplexBench data flow through the existing `CaseSource` Protocols without `NotImplementedError`.
> 5. The `forget that` command persists a bi-temporal tombstone visible in `retrieve()` queries (shipped via PR #253 — backfilled into v0.2 scope).
> 6. The 6 opt-in real adapters from the post-v0.1j stub-replacement sweep (CLIP, GroundingDINO, AV-conflict, MiniCPM-deictic, prosody-urgency, sentence-transformer) have an explicit default-on / default-off decision recorded.
> 7. The harness is hostable outside the manual-test rig (production server posture, replay-report export, durable session-blob storage).
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
- **OQ-4**: Default opt-in for the 6 v0.1j real adapters? **DEFER per-adapter** to v0.2 Task 12 (review acceptance criteria per adapter; flip defaults only where the adapter has proven low-risk on hardware budget AND high signal value).
- **OQ-5**: Tombstone for `forget that` in v0.2 scope? **YES** — already shipped via PR #253 on 2026-05-15; backfill into v0.2 scope as Task 0.
- **OQ-6**: Real BackgroundReasoner backend choice — MCP or LLM-direct? **BOTH** (Anchor 2) — MCP primary, LLM fallback, fake preserved.
- **OQ-7**: Production deployment in v0.2? **NO** (Anchor 5) — separate v0.3 scope.
- **OQ-8**: Spec amendment in v0.2? **NO** — spec remains FROZEN. All v0.2 work is capability-upgrades to existing stages.

## Tasks (15 tasks across 6 waves)

### Wave 0 — Already shipped (backfilled into v0.2 scope)

- **Task 0a (shipped).** `forget that` → bi-temporal tombstone via SleepTimeAgent. PR #253.
- **Task 0b (shipped).** 6 real-adapter opt-in CLI flags for the live pipeline (live-wiring PR in flight at time of v0.2 draft).

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

### Wave 4 — Eval Phase C (live examiner) — gated on Wave 2

- **Task 15.** `LiveExaminerCaseSource` implementing the eval case-source Protocol against a recorded session's event log. Uses diarization output to attribute utterances to specific speakers; uses Tier-B replayer to reconstruct policy decisions. Synthetic-mode default; real-mode raises `NotImplementedError` until paired with a recorded-session fixture set.

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

**Spec gates (production-quality, all stages):**

| Metric | Gate |
|---|---|
| `background_reasoner_budget_exhaustion_rate` | < 0.05 (tolerable) |
| `background_reasoner_summary_set_context_attribution_rate` | == 1.0 |
| `diarization_latency_ms_p95` | < 50ms |
| `diarization_speaker_continuity_addressing_accuracy` | > 0.85 (vs Phase C ground truth) |
| `diarization_false_speaker_change_rate` | < 0.05 |
| `tombstone_provenance_chain_closure_rate` | == 1.0 |
| `real_mode_eval_score_replay_match_rate` | == 1.0 (deterministic given pinned `revision=`) |

**Harness-derived (production-quality):**

| Metric | Gate |
|---|---|
| `dataset_row_skip_rate` | < 0.05 (real-mode CANDOR / FDB) |
| `production_session_uptime_24h_p99` | > 0.99 (informational at v0.2; gated in v0.3) |
| `policy_replay_exact_v0_2_final` | == 1.0 |

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
- Live-wiring (Task 0b): coder in flight at time of draft (post-v0.1j sweep).
- Model-stack reference: `docs/model-stack.md`.
- Manual-test handbook: `docs/manual-test-handbook.md` (post-v0.1j refresh).
- Session-handoff doc: `docs/project-progress-2026-05-15.md`.

## §Risks

1. **Two-step POLICY_VERSION bump migration fatigue.** Every fixture pinning the version updates twice. Mitigation: tooling that finds all `"v0.1j"` literals and proposes the migration; manual verify per fixture.
2. **Pyannote acoustic feedback edge cases.** TTS re-captured by mic may not be cleanly suppressed by the mute-window in all room conditions. Mitigation: ground-truth recording per Wave 2 Task 8 contract test.
3. **MCP server reliability.** External tool servers may flake. Mitigation: LLM fallback + per-server reliability monitor + `signal_producer_fallback` event emission.
4. **Real-mode benchmark run-time.** Hours, not minutes. Mitigation: `--limit` flag for smoke; full runs are operator-scheduled.
5. **Default-on cascade surprise.** Flipping `--enable-clip-scene` to default-on changes resource use for every fresh `manual_test_console.server` invocation. Mitigation: per-adapter PR with explicit changelog entry; revert path is one-line.

## §Out of scope (deferred to v0.3+)

- Production deployment posture (Docker, systemd, k8s, secrets, log shipping).
- Multi-host horizontal scaling.
- Cross-session speaker recognition (single-session at v0.2).
- Recursive reasoner-calling-reasoner.
- Custom MCP server implementation.
- Real-mode synthetic-mode score parity (numeric scores legitimately differ).
- v0.1g Task 21 RCT methodology doc (advisory; not gating; revisit at v0.3).
