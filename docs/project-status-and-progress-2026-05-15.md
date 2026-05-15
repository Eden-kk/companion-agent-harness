# Project status and progress — 2026-05-15

> Factual snapshot of the companion-agent-harness repository at commit `784990b` (post-PR-#142 merged). Audience: a peer-reviewing model that has not seen the codebase. Self-contained — no external context required.

---

## §1 — Repository map

Tree of the implementation directories. One-line annotation per file.

### `companion_harness/` (the harness package — adapters, schemas, policy)

| File | Purpose |
|---|---|
| `__init__.py` | Package docstring; points at `docs/architecture-v0.1.md`. |
| `asr_adapter.py` | `ASRModel` Protocol — the seam for whisper / faster-whisper / cloud ASR. |
| `asr_faster_whisper.py` | `FasterWhisperASRModel` — whisper-tiny.en, greedy decoding (`temperature=0`, `beam_size=1`). |
| `audio_output_controller.py` | `AudioOutputController` — playback lifecycle, barge-in stop, generation cancel. Emits `assistant_audio_*` events. |
| `backchannel_asr_lexicon.py` | `ASRLexiconBackchannelModel` — whisper-tiny + lexicon backchannel classifier. |
| `backchannel_classifier.py` | `BackchannelClassifier` adapter; `emit_threshold=0.3` (PR #134 Finding 3 fix). |
| `causal_graph.py` | `CausalGraph` — offline DAG reconstruction from `caused_by[]`; gates `test_causal_graph_completeness`. |
| `companion_state.py` | `CompanionState` schema; v0.1d. |
| `core_user_profile_store.py` | One of 4 memory stores; bi-temporal `valid_to` semantics per issue #104. |
| `decision_trace_store.py` | `DecisionTraceStore` — persists `DecisionTrace` JSONs per `decision_id`. |
| `deictic_detector.py` | `DeicticDetector` stub; v0.1c Task 5 skeleton. |
| `episodic_memory_store.py` | One of 4 memory stores; v0.1e Task 5. |
| `event_logger.py` | Async non-blocking `EventLogger`; `log_drop_or_degrade` on backpressure (invariant #10). |
| `fixtures/` | Recorded contract-test fixtures. |
| `foreground_model.py` | `DuplexModel` / `StreamingDuplexModel` Protocols; `ForegroundModel` wrapper. |
| `foreground_model_minicpm.py` | `MiniCPMStreamingModel` — MiniCPM-o 4.5 with `init_audio=True`, `init_tts=False`. b200-only. |
| `input_ingest.py` | `InputIngest` adapter; emits `raw_audio_chunk` / `raw_video_frame` events. |
| `live_loop_metrics.py` | Latency/gate computation: direct-question p50/p95, barge-in p95. |
| `memory_manager.py` | `MemoryManager` Protocol. |
| `privacy_gates.py` | Privacy-mode gate helpers used by `MemoryManager`. |
| `realtime_loop.py` | Legacy non-streaming live-loop (`_signals_to_policy_inputs`). |
| `realtime_orchestrator.py` | **`StreamingRealtimeOrchestrator`** — T0/T1/T2/T3/T4 async task graph. The live-loop heart. |
| `reason_codes.py` | `ReasonCode` enum (Stage 3 gap audit complete; Stage 5 additions for v0.1f). |
| `replay.py` | Replay runner shim. |
| `replay_privacy_policy.yaml` | Per-mode log-level + retention table from spec Part 6. |
| `schemas.py` | Concrete dataclasses for `Event`, `PolicyInputs`, `SpeakDecision`, etc. |
| `semantic_relational_store.py` | One of 4 memory stores; v0.1e Task 9. |
| `session_state_store.py` | One of 4 memory stores; tombstone semantics tracked by issue #105. |
| `sleep_time_agent.py` | `SleepTimeAgent` — Mem0-style extract→update on commit; v0.1e Task 10. |
| `smart_turn_pipecat.py` | `PipecatSmartTurnModel` — Pipecat Smart Turn v3 ONNX. |
| `speak_policy.py` | **`decide()`** — pure policy function; `_BLOCKING_SOCIAL_MODES`, `_BACKCHANNEL_THRESHOLD`, threshold-path reconstruction. |
| `speak_policy_config.py` | Spec Part 6 Stage 3 YAML encoded as dataclasses. |
| `tool_progress.py` | v0.1f schema foundation (PR #128). |
| `tts_adapter.py` | `TtsAdapter` Protocol. |
| `tts_kokoro.py` | `KokoroTtsAdapter` — Kokoro-82M-ONNX, 24kHz mono PCM16. |
| `turn_detector_smart.py` | `SmartTurnDetector` — silence-candidate-gated invocation. |
| `turn_detector_vad.py` | `VADDetector` — per-frame; `speech_threshold=0.5`, `silence_onset_ms=300`. |
| `v0_1e_event_schema.py` | v0.1e new event types. |
| `v0_1f_event_schema.py` | v0.1f new event types (PR #128). |
| `v0_1g_event_schema.py` | v0.1g new event types (PR #129). |
| `vad_silero.py` | `SileroVADModel` — Silero VAD ONNX wrapper. |
| `vision_sidecar.py` | `VisionSidecar` — 60s ring buffer + grounding stub + live-loop pairing buffer (PR #141). |

### `manual_test_console/` (live-pipeline server + static page)

| File | Purpose |
|---|---|
| `__init__.py` | Package marker. |
| `index.html` | Static capture+display page; AudioWorklet PCM chunker; AudioContext playback scheduling. |
| `live_pipeline.py` | `build_live_pipeline()` factory; `_live_policy_inputs_builder` (post-#142 spec-aligned); CPU stubs. |
| `server.py` | aiohttp app; 5 routes (`/`, `/healthz`, `/ws/ingest`, `/ws/display`, `/ws/audio_out`); model factories. |

### `tests/` (selected)

85 test files in total. Key contract tests:
- `test_policy_replay_exact.py`, `test_decision_provenance.py`, `test_causal_graph_completeness.py` — Stage 0.
- `test_thinking_pause.py`, `test_barge_in.py`, `test_backchannel_survival.py`, `test_direct_question_latency.py`, `test_explicit_turn_handoff.py`, `test_no_latency_regression.py`, `test_false_interruption_rate.py` — Stage 1.
- `test_audio_visual_conflict.py`, `test_current_frame_grounding.py`, `test_deictic_continuity.py`, `test_recent_visual_memory.py`, `test_hallucination_resistance.py`, `test_ambiguous_deictic_refusal.py`, `test_temporal_event_order.py` — Stage 2.
- `test_not_addressed_to_me.py`, `test_cooking_alert.py`, `test_creative_focus_silence.py`, `test_aesthetic_cooldown.py`, `test_eou_invariant_under_mode.py` — Stage 3.
- `test_explicit_remember.py`, `test_explicit_forget.py`, `test_explicit_hard_delete.py`, `test_correction.py`, `test_why_did_you_say_that.py`, `test_no_camera_memory.py`, `test_sensitive_conversation_retention.py`, `test_cross_adapter_retrieval.py` — Stage 4.
- `test_live_policy_inputs_builder_spec_aligned.py` — added by PR #142.
- `test_manual_test_console.py`, `test_manual_test_ingest.py`, `test_manual_test_live_pipeline.py` — manual-test server.
- `test_realtime_orchestrator*.py` — orchestrator unit tests.
- `test_v0_1f_event_schema.py`, `test_v0_1g_event_schema.py` — Stage 5/6 schema foundation.

### `docs/`

| File | Purpose |
|---|---|
| `architecture-v0.1.md` | The frozen spec. 1081 lines. |
| `implementation-config.yaml` | Per-Part-9 model bindings. |
| `manual-test-handbook.md` | Operator handbook for the manual-test rig. |
| `manual-test-findings-2026-05-15.md` | Findings from the 2026-05-15 manual-test session. |
| `plan-memory-wiring-followup.md` | Converged plan for memory wiring. Not yet implemented. |
| `plan-vision-sidecar-wiring.md` | Converged plan for VisionSidecar wiring (PR #141 implements Anchors 1-4). |
| `remote-dev.md` | b200 workflow notes. |
| `roadmap-v0.1e-draft.md` | v0.1e roadmap (tagged). |
| `roadmap-v0.1f-draft.md` | v0.1f roadmap (Task 1 shipped; 2-15 pending). |
| `roadmap-v0.1g-draft.md` | v0.1g roadmap (Tasks 1+2 shipped; 3-21 pending). |
| `v0_1e_memory_state_fixture_schema.md` | v0.1e memory-state fixture schema. |
| `adr/` | Architectural Decision Records (currently sparse). |

### `scripts/`

| File | Purpose |
|---|---|
| `live_loop_latency_report.py` | Latency analyzer (status_reason fields tracked by issue #107). |
| `v0_1a_replay_report.py` | v0.1a milestone replay report. |
| `v0_1b_replay_report.py` | v0.1b milestone replay report. |
| `v0_1c_replay_report.py` | v0.1c milestone replay report. |
| `v0_1d_replay_report.py` | v0.1d milestone replay report. |
| `v0_1e_replay_report.py` | v0.1e milestone replay report. |

---

## §2 — Shipped milestones

### v0.1a (tag `v0.1a`, commit `c9e4ee5`)

VAD-only baseline. Stage 0 (replay + causal provenance) + Stage 1 (audio, VAD-only) + Stage 3 (two-action subset `{silence, full_response}`). Adapters wired: `EventLogger`, `TurnDetectorSuite` (just `VADDetector`), `SpeakPolicy`, `ForegroundModel`. Required contract tests passed: `test_barge_in`, `test_false_interruption_rate`, `test_direct_question_latency`, `test_explicit_turn_handoff`, `test_policy_replay_exact`, `test_decision_provenance`, `test_causal_graph_completeness`. `test_thinking_pause` deferred per issue #10 spec amendment (VAD alone cannot satisfy). Replay-report script: `scripts/v0_1a_replay_report.py`. Numeric gates green: orphan_action_count=0, policy_replay_match_rate=100%, barge-in p95 under spec gates.

### v0.1b (tag `v0.1b`, commit `9e23f45`)

Backchannel-aware EOU. Added `SmartTurnDetector` + `BackchannelClassifier`; `SpeakPolicy` adds the `backchannel` action type. Issue #20 amendment locks the spec's "OR" between detectors as **AND** (both required). Un-deferred tests: `test_thinking_pause`, `test_backchannel_survival`, `test_detector_ablation`. Replay-report script: `scripts/v0_1b_replay_report.py`.

### v0.1c (tag `v0.1c`, commit `0d5cc9b`)

Stage 2 — audio-video grounding. `VisionSidecar` ring buffer (60s), scene-change scorer Protocol, grounding-model Protocol, `DeicticDetector` skeleton. Un-deferred tests: `test_current_frame_grounding`, `test_deictic_continuity`, `test_recent_visual_memory`, `test_hallucination_resistance`, `test_ambiguous_deictic_refusal`, `test_audio_visual_conflict`, `test_temporal_event_order`. ReplayRun: 15/15 gates MET, 122 tests pass (PR #72). Replay-report script: `scripts/v0_1c_replay_report.py`.

### v0.1d (tag `v0.1d`, commit `a590d82`)

Stage 3 speak/silence policy + live-loop integration. `SpeakPolicy` expanded to the full 8-action set; reason-code enum extended; per-mode `alert_threshold` and `aesthetic_reaction_budget` dataclasses; live-loop metrics framework (`live_loop_metrics.py`). Un-deferred tests: `test_not_addressed_to_me`, `test_cooking_alert`, `test_creative_focus_silence`, `test_aesthetic_cooldown`, `test_eou_invariant_under_mode`. Replay-report script: `scripts/v0_1d_replay_report.py`.

### v0.1e (tag `v0.1e`, commit `775de74`)

Stage 4 — four-store memory + sleep-time agent. `SessionStateStore`, `CoreUserProfileStore`, `EpisodicMemoryStore`, `SemanticRelationalStore`; bi-temporal `valid_to` semantics (issue #104 contract); `SleepTimeAgent` extract→update; privacy-mode gates (issue #96 partial — `local_only` is a hard-raise); cross-adapter retrieval (`DuplexModel.set_context`). Un-deferred tests: `test_explicit_remember`, `test_explicit_forget`, `test_explicit_hard_delete`, `test_correction`, `test_why_did_you_say_that`, `test_no_camera_memory`, `test_guest_present_memory_gate`, `test_sensitive_conversation_retention`, `test_no_latency_regression`. Replay-report script: `scripts/v0_1e_replay_report.py`.

---

## §3 — In-flight tracks

### v0.1f — Stage 5 (tool routing)

Roadmap: `docs/roadmap-v0.1f-draft.md`. Status: **DRAFT** converged 2026-05-15.

- **Pinned success criterion.** "v0.1f succeeds when the system handles long-running tool calls without foreground blocking; tool calls cancel within 300ms on barge-in; foreground narration about tool progress is evidence-bound (only when a `ToolProgressEvent` exists in the log); filler budget is enforced (max 2 fillers per call, 4 seconds between, silence-wins-after-first-filler); v0.1e gates (memory) and all earlier gates remain green."
- **Anchor 1 (locked).** `ToolProgressEvent` payload + `progress_stage` alphabet (`Literal["started", "scanning", "aggregating", "completed", "cancelled"]`).
- **Anchor 2 (locked).** 5 new `tool_*` event types + 2 new `retention_policy_id` values + DAG ordering invariant.
- **Anchor 3 (locked).** `routing_tier: Literal["fast", "smart"]` recorded on `tool_call_dispatched`.
- **Anchor 4 (locked).** Filler-budget state machine on `ToolProgressEmitter`.
- **Task 1 shipped (PR #128).** Schema foundation for Stage 5.
- **Tasks 2-15.** Pending.

### v0.1g — Stage 6 (companion texture)

Roadmap: `docs/roadmap-v0.1g-draft.md`. Status: **DRAFT** converged 2026-05-15.

- **Pinned success criterion.** "ThinkerProposalGen emits proposals only (invariant #2); all proposals pass through `SpeakPolicy.decide()` (invariant #4); `aesthetic_reaction` content respects all eight rubric checks; per-mode proactivity budgets are enforced; the attachment-risk monitor flags concerning patterns with false-positive rate gated; the 1-week RCT methodology is defined as primary product eval (advisory; not a release gate); all earlier-stage gates remain green."
- **Anchor 1.** `RubricViolation` enum: 8 IDs on `ThinkerProposal.rubric_violations`.
- **Anchor 2.** `AttachmentRiskSignal` payload + `attachment_risk_signal` event_type.
- **Anchor 3.** `recent_shared_moments` = projection over `episodic_memory` (on-demand).
- **Anchor 4.** Rubric enforcement inside `SpeakPolicy.decide()` (visible at audit; bit-identical replay).
- **Tasks 1+2 shipped (PR #129).** Stage 6 schema + event types + reason codes.
- **Tasks 3-21.** Pending. Hard dependency on v0.1f's `proactivity_budget_remaining` alphabet (locked empty-set at v0.1f).

### Live-loop integration (cross-milestone)

All real components wired into the manual-test live pipeline. Below: the PR map.

| Component | Adapter | PR(s) |
|---|---|---|
| VAD | `SileroVADModel` (Silero ONNX) | PR #126 |
| SmartTurn | `PipecatSmartTurnModel` (Smart Turn v3 ONNX, CPU) | PR #126 |
| Backchannel | `ASRLexiconBackchannelModel` (whisper-tiny + lexicon); `emit_threshold=0.3` Finding 3 fix | PR #126, PR #134 |
| ASR | `FasterWhisperASRModel` (whisper-tiny.en, faster-whisper) | PR #136 |
| Foreground | `MiniCPMStreamingModel` (MiniCPM-o 4.5, `init_audio=True`) | PR #125 |
| TTS | `KokoroTtsAdapter` (Kokoro-82M-ONNX, 24kHz) | PR #127, PR #135 |
| Audio out | `WebSocketAudioSink` → `AudioOutBroker` → `/ws/audio_out` | PR #132 |
| VisionSidecar | wired into pipeline + `--enable-vision` flag (default OFF) | PR #141 |
| Addressing | spec-aligned `_live_policy_inputs_builder` (mechanical derivation from `social_mode`) | PR #140 stopgap → PR #142 spec-aligned |
| Live-console inline verdicts | `payload_inline` for `policy_decision` events (Finding 5 fix) | PR #133 |
| Handbook panel routing clarification (Finding 2 fix) | docs | PR #131 |

### Memory wiring (deferred)

Plan converged at `docs/plan-memory-wiring-followup.md`. Per-session stores under `<blob_dir>/<session_id>/memory/{session,core,episodic,semantic}/`. `DuplexModel.process_stream` extended with `context_items: tuple[MemoryItem, ...] = ()`. Not yet implemented in the live pipeline factory. Today: `episodic_store=None`, `semantic_store=None` → `retrieved_items=[]` always.

### VisionSidecar real models (deferred)

PR #141 wired VisionSidecar into the live pipeline (Anchors 1-4 of `docs/plan-vision-sidecar-wiring.md`). Anchor 5 — real scoring + grounding — is intentionally deferred. Today: `_NullSceneScorer` returns `0.0`, `_NullGroundingModel` returns `("", 0.0)` (`manual_test_console/server.py:115-126`). Scenarios I-L (deictic, audio-visual conflict, `no_camera_memory`) remain not-exercisable end-to-end.

---

## §4 — Recent debugging sessions

Findings 1-6 from `docs/manual-test-findings-2026-05-15.md`. The session was run by the project lead on b200; test companion was a fresh Claude Code session following `docs/manual-test-session-brief.md`.

### Finding 1 — Phase-1 server lacked orchestrator wiring

- **Severity.** Bug / scope mismatch.
- **Root cause.** Initial `manual_test_console/server.py:build_app()` only wired `InputIngest`; no VAD / SpeakPolicy / RealtimeOrchestrator imports.
- **Status.** Resolved by PR #125 (StreamingRealtimeOrchestrator + MiniCPM-o wired into the live pipeline). The "Phase 3" server build is now on `main`.

### Finding 2 — Panel-routing confusion (`raw_audio_chunk` rows)

- **Severity.** Confusion (not a bug).
- **Root cause.** `renderEvent` in `manual_test_console/index.html` correctly routes `payload_kind == raw_audio|raw_video` to `ingestRows` and everything else to `signalRows`. Operators were uncertain which panel held which event type.
- **Status.** Resolved by PR #131 (handbook §2.4 annotated with the panel-routing convention).

### Finding 3 — `log_drop_or_degrade` storm during silence

- **Severity.** Bug.
- **Root cause.** Backchannel classifier emitted a `backchannel_classification` event for every frame at ~31 Hz, saturating the EventLogger drain during silence. Invariant #10's backpressure path fired correctly (no silent loss), but the manual-test console became a lossy view.
- **Status.** Resolved by PR #134. Added `emit_threshold=0.3` to `BackchannelClassifier`: only emit events (and return TurnSignals) when `p_backchannel >= threshold`. Drain saturation eliminated.

### Finding 4 — Superseded by Finding 6

- **Status.** Original hypothesis (over-eager invariant #4/#8 violation during silence) was wrong. The decision-trace store showed zero `full_response` decisions across all 588 traces; the foreground/assistant pipeline activity observed during silence was speculative generation correctly gated to silence (`synthesis_skipped_no_proposal` rows). No invariant violation. Finding 6 is the real bug.

### Finding 5 — Live console couldn't surface `action_type` / `primary_reason_code`

- **Severity.** Bug (blocks operator debugging).
- **Root cause.** The Event JSON shipped over `/ws/display` for `policy_decision` only carried the audit envelope; `action_type` and `primary_reason_code` were in the on-disk `DecisionTrace` via `payload_ref: "decision_trace://..."` which the console didn't fetch.
- **Status.** Resolved by PR #133. `policy_decision` events now carry `payload_inline = {"action_type": ..., "primary_reason_code": ...}` (`realtime_orchestrator.py:486-505`). The console reads the inline fields and renders the verdict next to each row (`manual_test_console/index.html:160-167`).

### Finding 6 — Headline bug: harness cannot break silence

- **Severity.** Bug — load-bearing.
- **Root cause.** `user_addressed_agent` was never set to `True`. With `social_mode="default"` and `user_addressed_agent=False` defaults, every EOU resulted in `silence` with `NOT_ADDRESSED_TO_AGENT`. Across all 588 decision traces in the session: 530 silence, 58 backchannel, 0 `full_response`. The policy was correctly silencing because the upstream signal source was inert.
- **Status.** Root cause diagnosed; two-PR resolution:
  - **Stopgap (PR #140).** Hardcoded `user_addressed_agent=True` to unblock end-to-end voice testing. The harness could speak, but speech fired on every utterance (including ambient/background) — violating invariant #8 ("silence wins ties") for non-addressed speech.
  - **Spec-aligned (PR #142).** Replaced stopgap with mechanical derivation: `user_addressed_agent = (social_mode == "user_addressing_agent")`. Mode-field defaults switched from `"default"` to spec-enumerated values (`normal`/`user_addressing_agent`/`normal`/`normal`). The spec is silent on per-utterance derivation; mechanical derivation from `social_mode` (the spec's first-class addressing signal, line 799) is the most defensible interpretation absent a project-lead decision. Issue #139 tracks the open question.

---

## §5 — Open follow-up issues

Enumerated via `gh issue list --state open`. Currently open issues with one-paragraph summaries.

### #139 — Live pipeline: derive `user_addressed_agent` from real signal (replace stopgap from Finding 6)

The current implementation derives `user_addressed_agent` mechanically from `social_mode` (both are hardcoded `"user_addressing_agent"` in the single-user manual-test path). Always-True / always-derived-from-default is just as wrong as always-False for ambient/overheard speech — it tells the policy that every utterance in the room is addressed at the agent. Three named candidates: ASR-keyword heuristic (cheap; recall-limited), foreground-proposal-derived flag (MiniCPM-o emits a classification token), trained addressing classifier (whisper transcript + lightweight model). Milestone: v0.1g+ candidate.

### #96 — Adapter-routing validation for privacy modes (v0.1f)

Spec Part 7 lines 805-836 define `privacy_mode_compatibility`. `local_only` mode (lines 811-818) disallows cloud `ForegroundModel`, cloud ASR, cloud memory storage, any cloud tool dispatcher. v0.1e handles this as a hard-raise at `MemoryManager.commit()`; v0.1f should implement full cross-adapter validation via an adapter registry and startup/runtime checks. Scope covers all 7 privacy modes from spec Part 7 line 797.

### #105 — `session_state_store.forget()` removes items instead of retaining tombstone (spec violation)

`session_state_store.py` currently calls `self._items.pop(item_id, None)` in `forget()`. Spec lines 523-527 say `test_explicit_forget` expects a "minimal audit tombstone retained" with `valid_to` set and `superseded_by` unset. Reference implementation: `core_user_profile_store.py:91-95`. Violates invariant #3 (memory provenance).

### #107 — Analyzer: distinguish 'trace_dir missing' from 'no full_response decisions' in status_reason

`live_loop_metrics.py:138-141` and `scripts/live_loop_latency_report.py:141` collapse two distinct scenarios into the same `status_reason="no_full_response_decisions_in_session"` — operator forgot `--trace-dir` vs session genuinely had no `full_response` decisions. Proposed split: `trace_dir_not_provided` / `trace_dir_missing` / `no_full_response_decisions_in_session`. Non-blocking.

### #113 — SleepTimeAgent: emit `memory_commit_skipped` for all store-level `_SkipCommit` outcomes

PR #112 introduced privacy gates in `MemoryManager.commit()`. The agent → store interaction has an asymmetry: `guest_present` correctly emits `memory_commit_skipped` before the store call; `no_memory` / `sensitive_conversation` silently catch `_SkipCommit` and emit a false `memory_commit_completed`. Two log-DAG gaps result. Proposed fix: stores return a `CommitResult` enum (or move `_SkipCommit` to public API). Violates invariant #1.

### #104 — Bi-temporal `MemoryItem.valid_to` semantic — contract for all MemoryManager stores

Locks the bi-temporal contract: an item is active iff `valid_to is None OR valid_to > now_utc()` AND `superseded_by is None`. PR #101 (Task 7) is the reference implementation; PR #102 (Task 8) was patched to align. Forget vs. correction distinguished. This is a contract record, not a defect.

### #20 — Spec amendment: Part 8 v0.1b detector scope — 'OR' should be 'AND'

Records the project-lead decision (2026-05-14) to treat spec Part 8 v0.1b's "SmartTurnDetector OR lightweight backchannel classifier" as **AND**. Pipecat Smart Turn v3 is binary end-of-turn only; satisfying both `test_thinking_pause` and `test_backchannel_survival` requires both detectors. Spec is not edited (same handling as #10); this issue is the authoritative amendment record.

### #10 — Spec defect: `test_thinking_pause` mandated at v0.1a but requires v0.1b's SmartTurnDetector

Spec contradiction: v0.1a is VAD-only by Part 8 but `test_thinking_pause` requires `SmartTurnDetector` (deferred to v0.1b). Resolution (2026-05-14): `test_thinking_pause` is deferred to v0.1b alongside `test_backchannel_survival` and `test_detector_ablation`. Spec not edited; this issue is the amendment record. `tests/test_thinking_pause.py` was un-skipped at v0.1b.

---

## §6 — Spec-amendment record

Spec `docs/architecture-v0.1.md` is frozen at v0.1. Amendments are recorded as GitHub issues, not edits to the spec file. Currently recorded amendments:

- **Issue #20** — Part 8 v0.1b detector scope: "OR" → "AND" (both `SmartTurnDetector` and `BackchannelClassifier` required for v0.1b).
- **Issue #10** — `test_thinking_pause` deferred from v0.1a to v0.1b (VAD alone insufficient).

No other amendments recorded in the open-issues list as of 2026-05-15. Spec-silent areas surfaced by `docs/design-audio-path-v0.1f.md` §4 (per-utterance `user_addressed_agent` derivation, `social_mode` detector, `urgency_score` derivation, etc.) await project-lead decisions before becoming amendment records.

---

## §7 — Known concerns / latent risks

### MiniCPM `_memory_context` concurrent-session race

`MiniCPMStreamingModel.set_context` (`foreground_model_minicpm.py:93-94, 146`) writes to `self._memory_context`. The instance is a singleton at server startup (`manual_test_console/server.py:_load_minicpm_streaming_model`). If two concurrent `/ws/ingest` sessions both set context simultaneously, the second overwrites the first with no isolation. Gatekeeper concern from PR #125. Mitigated by the `docs/plan-memory-wiring-followup.md` Anchor 1 design: `set_context()` becomes an advisory stash; the per-call `context_items` keyword is the authoritative path. Not yet implemented.

### Shared-worktree race caused plan-drafts data loss

The 2026-05-15 v0.1f / v0.1g plan-drafts were lost in a shared-working-tree race between concurrent fix-coder agents. Recovered from agent notes (PR #130). Mitigation: new agent dispatches use `isolation: worktree` so each agent operates in its own filesystem subtree.

### `audio_out_chunks_sent` requires a `full_response` decision

`AudioOutBroker.publish` increments `chunks_sent` only when an actual audio chunk fires (`manual_test_console/server.py:191`). With Finding 6 unfixed, every decision was silence, so `chunks_sent` stayed at 0. PR #142 unblocked this counter — now non-zero when an EOU + `user_addressed_agent=True` chain completes.

### VRAM headroom on b200

MiniCPM-o with `init_audio=True` baseline ~28 GB VRAM. The `--enable-vision` flag adds the MiniCPM-o vision tower (`init_vision=True`) for an additional ~18 GB, totaling ~46 GB (`manual_test_console/server.py:761-766`). b200 has 191 GB total. Headroom is comfortable but a second vision-tower-bearing model (e.g., a separate grounding model) would tighten the budget. Risk record at `docs/plan-vision-sidecar-wiring.md` Top-2-risks.

### Stub vs real model parity

`use_stubs=True` mode replaces every model adapter with a CPU-only stub (`EnergyVADModel`, `SilenceSmartTurnModel`, `ZeroBackchannelModel`, `EmptyTranscriptASRModel`, `NoopTtsAdapter`). Useful for tests / dev machines without GPU but means a green test run does not guarantee real-model behavior. Real-model integration tests (`tests/test_real_detectors.py`, `tests/test_tts_kokoro_real.py`, `tests/test_minicpm_streaming_duplex.py`) run on b200 only.

---

## §8 — Current testable state

Recorded as observed at the time of this writing (2026-05-15).

- **Server.** Running on b200 (PID 1990890, port 8800) in audio-only mode (vision flag OFF) after the most-recent restart.
- **Banner labels.**
  - VAD: Silero (ONNX)
  - SmartTurn: Pipecat SmartTurn v3 (ONNX, CPU)
  - Backchannel: whisper-tiny + lexicon (emit_threshold=0.3)
  - TTS: Kokoro-82M-ONNX
  - ASR: whisper-tiny.en (faster-whisper)
  - Foreground: MiniCPM-o 4.5 (b200 GPU)
- **Audio-input scenarios.** All `manual-test-handbook.md` §2.5 scenarios A-H are exercisable. Scenarios C, F, H (which previously required `user_addressed_agent=True`) are now reachable post-#142.
- **Vision scenarios I-L.** Require `--enable-vision` flag; server must be restarted. Once enabled, frame ingest + VisionSidecar buffering work end-to-end; scoring + grounding return stub values, so cosmetic display works but no real visual-grounded responses can fire.
- **Voice-back.** Closed end-to-end (browser mic → orchestrator → MiniCPM-o → Kokoro → `/ws/audio_out` → browser AudioContext). `full_response` decisions fire when EOU is confirmed and `user_addressed_agent=True` (mechanically derived from single-user `social_mode`).
- **Decision traces.** Persisted to `/tmp/manual_test_blobs/decision_traces/<decision_id>.json`. 588 traces from prior sessions + however many have accumulated since #142 landed. Survey can be run with `find /tmp/manual_test_blobs/decision_traces -name '*.json' | xargs jq -r '.counterfactuals.action_selected' | sort | uniq -c`.
- **Memory.** Stores are `None` in the live pipeline factory; `retrieved_items=[]` always; `memory_write_candidate` events emit but commit nowhere. Plan converged; implementation deferred.

---

*Project status snapshot 2026-05-15 against post-PR-#142 working tree (`784990b`). All file:line citations refer to that commit unless explicitly noted.*
