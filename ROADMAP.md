# Roadmap

## MILESTONE STATUS TABLE

| Milestone | Scope | Status | Tag |
|---|---|---|---|
| v0.1a | Stage 0 + minimal Stage 1 (VAD-only baseline, causal graph, replay) | **feature-complete** on main | `v0.1a` — project-lead tag pending |
| v0.1b | Backchannel-aware EOU (`SmartTurnDetector`, `BackchannelClassifier`) | **feature-complete** on main | `v0.1b` — project-lead tag pending |
| v0.1c | Stage 2 audio-video grounding (`VisionSidecar`, `DeicticDetector`) | **feature-complete** on main | `v0.1c` — project-lead tag pending |
| v0.1d | Stage 3 speak/silence policy + live-loop integration | **feature-complete** on main | `v0.1d` — project-lead tag pending |
| v0.1e | Stage 4 four-store memory + sleep-time agent | **feature-complete** on main | `v0.1e` — project-lead tag pending |
| v0.1f | Stage 5 background reasoning & tool routing | **feature-complete** on main (PR #220) | `v0.1f` — project-lead tag pending |
| v0.1g | Stage 6 companion texture (`ThinkerProposalGen`, aesthetic-reaction rubric, attachment-risk monitor) | **feature-complete** on main (PR #218) | `v0.1g` — project-lead tag pending |
| v0.1h | Live-loop completion (manual-test handbook ×12 scenarios end-to-end) | **feature-complete** on main | `v0.1h` — project-lead tag pending |
| v0.1i | Operator surface (dashboard, threshold-tuning, operator docs) | **feature-complete** on main | `v0.1i` — project-lead tag pending |
| v0.1j | Final-product signal routing (real producers, no-stub-constant gate) | **feature-complete** on main (PR #217) | `v0.1j` — project-lead tag pending |
| Eval Phase A | eval-core + harness_native (CLI, schemas, reporters, 3 contract tests) | **feature-complete** on main (PR #208) | n/a — eval track |

---

## SHIPPED MILESTONES

### v0.1a — COMPLETE (tagged `v0.1a`)

The v0.1a MVP was the **VAD-only baseline**: every utterance has a cause, every policy decision is replayable, barge-in stops the assistant, and direct questions are answered promptly. All 8 required contract tests passed; all 9 numeric gates met. Stage 0 (causal graph + replay privacy) and Stage 1 (VAD-only) were enabled.

### v0.1b — COMPLETE (tagged `v0.1b`)

Added backchannel-aware EOU: `SmartTurnDetector` + `BackchannelClassifier` (both required per spec amendment in issue #20), the `backchannel` action type in `SpeakPolicy`, and the `test_thinking_pause`, `test_backchannel_survival`, `test_detector_ablation` contract tests. All v0.1a tests still pass. Stages 0–1 contract tests green. Full task breakdown: `docs/roadmap-v0.1b-draft.md`.

### v0.1c — COMPLETE

Stage 2 — **Audio-video grounding.** `VisionSidecar` (ring buffer, privacy gate), `DeicticDetector` Protocol + skeleton, `DuplexModel` Protocol extended for optional `video_frame` input, Stage 2 `ReasonCode` members (`DEICTIC_AMBIGUOUS`, `VISUAL_LOW_CONFIDENCE`, `AUDIO_VISUAL_CONFLICT`). All 7 Stage 2 contract tests green. Full task breakdown: `docs/roadmap-v0.1c-draft.md`.

### v0.1d — COMPLETE

Stage 3 — **Speak/silence policy (full)** + live-loop integration. Full action set (`short_reaction`, `clarification`, `alert`, `tool_status`, `aesthetic_reaction`, `backchannel`); per-mode proactivity budgets; `RealtimeOrchestrator` wired end-to-end; `DecisionTrace` schema + `decision_id` linkage scaffolded (production deferred to v0.1e per issue #34). All Stage 3 contract tests green.

### v0.1e — COMPLETE

Stage 4 — **Four-store memory + sleep-time agent.** `session_state`, `core_user_profile`, `episodic_memory`, `semantic_relational` stores; `SleepTimeAgent` async on non-realtime path; `DecisionTrace` production wired into `SpeakPolicy.decide()`; `retrieval_used` field; privacy-mode gates (`no_memory`, `no_camera_memory`, `guest_present`, `sensitive_conversation`); `test_why_did_you_say_that` passing. All 11 Stage 4 contract tests green. Full task breakdown: `docs/roadmap-v0.1e-draft.md`.

### v0.1f — COMPLETE (feature-complete on main; tag pending)

Stage 5 — **Background reasoning & tool routing.** `ToolRouter` Protocol (fast + smart-path), `ToolProgressEmitter`, `ToolProgressEvent` payload shape + locked `progress_stage` alphabet, foreground non-blocking on tool calls, barge-in cancels dispatch within 300 ms p50, filler budget enforced (invariant #9), all 6 Stage 5 contract tests green. `POLICY_VERSION` bumped `v0.1f → v0.1j` (single bump subsuming v0.1f–v0.1g behavior changes) by PR #217. Full task breakdown: `docs/roadmap-v0.1f-draft.md`.

### v0.1g — COMPLETE (feature-complete on main; tag pending)

Stage 6 — **Companion texture.** `ThinkerProposalGen` emits proposals only (invariant #2); all proposals pass through `SpeakPolicy.decide()` (invariant #4); `aesthetic_reaction` content gated by 8-check rubric; per-mode proactivity budgets enforced; `AttachmentRiskMonitor` flags concerning patterns; 1-week RCT methodology defined as advisory. All Stage 6 harness-derived gates green (`rubric_compliance_rate`, `aesthetic_reaction_cooldown_compliance_rate`, `recent_shared_moments_attribution_rate`, `thinker_direct_speech_violation_count`). Full task breakdown: `docs/roadmap-v0.1g-draft.md`.

### v0.1h — COMPLETE (feature-complete on main; tag pending)

**Live-loop completion.** Manual-test handbook 12 scenarios exercise the full pipeline end-to-end (real audio in/out, real video in when camera attached). Four-store memory architecture receives writes and serves retrievals during live sessions. `VisionSidecar` receives per-session video frames. `SpeakDecision.response_content_source` populated on every decision. Manual-test findings document closed with zero critical open findings. Full task breakdown: `docs/roadmap-v0.1h-draft.md`.

### v0.1i — COMPLETE (feature-complete on main; tag pending)

**Operator surface.** Threshold-tuning dashboard at `GET /`, 12 Tier-B keys live-tunable via `POST /config/patch` with `config_change` + `operator_action` event emission, dashboard reflects current values on page load, manual-test handbook documents dashboard workflow, operator docs refreshed with canonical-venv pinning + b200 reference + gatekeeper-loop convergence pattern. All in-flight dashboard PRs (#143–#155) merged. Full task breakdown: `docs/roadmap-v0.1i-draft.md`.

### v0.1j — COMPLETE (feature-complete on main; tag pending)

**Final-product signal routing.** Every `PolicyInputs` field is either (a) populated by the final-product producer the spec names, or (b) populated by a stub returning a deterministic default AND tagged `# UNAVAILABLE: <issue>` AND covered by a Stage-0 contract test that fails if the marker is removed without wiring a real producer. `test_no_stub_constants` and `test_unavailable_markers_have_issues` are the two new gates. `POLICY_VERSION = "v0.1j"`. Full task breakdown: `docs/roadmap-v0.1j-draft.md`.

### Eval Phase A — COMPLETE (feature-complete on main)

**eval-core + harness_native.** `companion_harness/evals/` skeleton (6 protocols, `BenchmarkAdapter`), `harness_native` adapter wrapping 4 existing contract tests as `EvaluationCase`s, JSON + MD reporters, `[eval]` optional extra in `pyproject.toml`, `eval-quickstart.md`. Three new contract tests: `test_runtime_does_not_import_evals` (import-direction gate), `test_eval_run_produces_event_log`, `test_eval_run_replay_safe` (xfail — Tier-B wiring deferred to Phase A.5). Full task breakdown: `docs/roadmap-eval-draft.md`.

---

## CURRENT MILESTONE: v0.2 (production-quality)

v0.2 takes the harness from *demonstrable* (v0.1 skeleton complete) to *deployable* (real reasoning, attribution, benchmarks, persistence — without operator workarounds). No new spec stages; capability-upgrades to existing ones.

- **Eval Phase A.5** — ✅ complete on `main` (`SyntheticClock`, `DirectAudioInputFeeder`, `FixtureScenarioDriver`, bit-identical Tier-B replay all shipped and tested). Was the Wave 0′ prerequisite for v0.2.
- **v0.2 roadmap** — `docs/roadmap-v0.2-draft.md` (in PR #254 until merged; v0.2 supersedes the prior v0.1c-stage scoping that lived in this section).

**Wave status:**
- Wave 0 (backfilled-shipped): tombstone for `forget that` (#253), 6-adapter live-wiring (#255), barge-in-vs-self fix (#271).
- Wave 0′ (prerequisite): Eval Phase A.5 — ✅ complete.
- Wave 2 (Diarization + v0.1k bump): ✅ feature-complete on `v0.2b-diarization-and-v0.1k` branch (pending merge).
- Wave 6 (Deployability): ✅ feature-complete on `v0.2f-deployability` branch — replay export, tar bundle, blob rotation, /healthz extensions (per-adapter readiness, GPU memory, event-rate counters), `scripts/v0_2_replay_report.py`.
- Waves 1, 3, 4, 5 (Reasoner, Benchmark loaders, Eval Phase C, Default-on): not yet dispatched.

(The v0.1c milestone referenced here historically has long shipped — see SHIPPED MILESTONES above. The Eval Phase A.5 sub-section in CURRENT ACTIVE WORK below is now stale; closing as complete in the same change.)

---

## CURRENT ACTIVE WORK

### v0.2 Wave 1 dispatch (next ramp)

v0.2 Wave 1 is real BackgroundReasoner (MCP-primary backend). Each wave starts with a fresh plan-critic'd execution plan written at dispatch time, grounded in then-current `main`. Open the wave by writing `docs/plan-v0.2-wave-1-execution.md` and running it through `/plan-review` to convergence.

### v0.1h Wave 4 — operator handbook walks (outstanding)

Manual-test handbook scenario walks that exercise the full v0.1h live-loop with the dashboard active. Documented in `docs/plan-v0.1h-execution.md`.

### v0.1[a-j] tags (project-lead actions)

All v0.1 milestones are feature-complete; the project lead applies the git tags.

---

## §NEXT TASKS

Pick the lowest-numbered item that isn't done. Do that one. Stop.

1. **Project-lead: apply tags `v0.1a` through `v0.1j`** in order. All milestones are feature-complete on main. Each tag is the project lead's action; scripts explicitly do NOT apply tags.

2. **Eval Phase A.5 — `SyntheticClock`.** `Task A.5-1`: monotonic-counter clock that the orchestrator can consume instead of `time.monotonic_ns()`. Success: orchestrator under SyntheticClock produces deterministic `mono_ms` timestamps in events.

3. **Eval Phase A.5 — `DirectAudioInputFeeder`.** `Task A.5-2`: pumps audio bytes into orchestrator's audio_in queue bypassing WebSocket. Success: orchestrator consumes feeder-injected audio without a WebSocket connection.

4. **Eval Phase A.5 — `FixtureScenarioDriver`.** `Task A.5-3`: wires SyntheticClock + DirectAudioInputFeeder + per-case fixture path → `ReplayRun`. Success: CLI can ingest a WAV fixture, run it through the orchestrator, produce a `ReplayRun`.

5. **Eval Phase A.5 — Tier-B replay contract test.** `Task A.5-4`: `test_eval_case_replay_bit_identical` — run a fixture twice, assert bit-identical Tier-B `SpeakDecision` sequences. Removes xfail on `test_eval_run_replay_safe`.

6. **Eval Phase B1 — CANDOR distributional probe.** Dispatch when Phase A.5 is complete. Full scope: `docs/roadmap-eval-draft.md` §Phase B1.

7. **Eval Phase B2 — Full-Duplex-Bench v1/v1.5 static + `FailureSliceExtractor`.** Can run in parallel with B1. Full scope: `docs/roadmap-eval-draft.md` §Phase B2.

8. **Cross-PR cleanup work-items** (carry-forward from ledger):
   - Watch-item #23 (Phase A.5): remove `test_eval_run_replay_safe` xfail when `replay.py` callable API is wired.
   - Watch-item #24 (Phase A.5): fill `FixtureScenarioDriver.run()` `NotImplementedError("Phase A.5")`.
   - `manual_test_critical_findings_open` sentinel for `docs/manual-test-findings-v0_1f.md`, `v0_1g.md`, `v0_1j.md` — requires operator manual-test passes.
   - `final_product_producer_invocation_rate >= 0.95` gate in v0.1j replay report — NOT_MEASURED locally; requires b200 CUDA session with real MiniCPM weights.

---

## Later stages — all shipped as of v0.1g

- **Stage 2 — Audio-video grounding** — COMPLETE (v0.1c).
- **Stage 3 — Speak/silence policy (full)** — COMPLETE (v0.1d).
- **Stage 4 — Memory** — COMPLETE (v0.1e). Four-store schema with provenance per item. `SleepTimeAgent` async. `forget` / `delete` / `why did you say that?` working.
- **Stage 5 — Background reasoning & tool routing** — COMPLETE (v0.1f). Two-tier MCP path. Foreground narration evidence-bound to `ToolProgressEvent`. Tool calls cancel within 300 ms on barge-in.
- **Stage 6 — Companion texture** — COMPLETE (v0.1g). `ThinkerProposalGen` emits proposals only. `aesthetic_reaction` respects 8-check rubric. Attachment-risk monitor active.

---

## Decision log

Add entries as ReplayRuns produce them. Each entry: date, what changed, why, which test regressed/passed.

- **2026-05-15** — POLICY_VERSION single-bump `v0.1f → v0.1j` (PR #217). v0.1g behavior changes (threshold-path additions for `aesthetic_reaction:rubric_blocked` and `attachment_risk:dampen_blocked` in PRs #207/#212) landed before the bump; a separate v0.1g bump was redundant. Monotonic POLICY_VERSION discipline enforced — no PR may decrement below current main value.
- **2026-05-15** — Eval track established as a lateral, infrastructure-pole track (`docs/roadmap-eval-draft.md`). Anchors 1+2 (import-direction + optional extra) prevent entanglement with v0.1* milestones.
