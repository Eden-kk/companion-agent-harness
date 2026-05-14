# Roadmap

## CURRENT MILESTONE: v0.1a

The v0.1a MVP is the **VAD-only baseline**. It exists to prove the substrate: every utterance has a cause, every policy decision is replayable, barge-in stops the assistant, thinking pauses do not trigger premature responses (thinking-pause capability deferred to v0.1b — see issue #10), and direct questions get answered promptly. The texture work (Stage 6) does not start until v0.1a passes.

### Adapters enabled

- `EventLogger` (with `ReplayPrivacyPolicy`)
- `TurnDetectorSuite` (one detector: `VADDetector`)
- `SpeakPolicy` (output set restricted to: `silence`, `full_response`)
- `ForegroundModel` (one instantiation; see `docs/implementation-config.yaml`)

### Stages enabled

- Stage 0 — fully (causal graph + replay privacy)
- Stage 1 — VAD-only, minimal
- Stage 3 — two-action subset `{silence, full_response}`

### Stages NOT enabled

- Stage 2 (vision) — disabled
- Stage 4 (memory) — session-only, no persistence
- Stage 5 (tools) — disabled
- Stage 6 (texture) — disabled

### Required contract tests

- `test_thinking_pause` [DEFERRED to v0.1b — see issue #10]
- `test_barge_in`
- `test_false_interruption_rate`
- `test_direct_question_latency` (positive responsiveness — prevents "passes by being sluggish")
- `test_explicit_turn_handoff` ("what do you think?" must respond promptly)
- `test_policy_replay_exact`
- `test_decision_provenance`
- `test_causal_graph_completeness`

Deferred to v0.1b (NOT required at v0.1a): `test_backchannel_survival`, `test_detector_ablation`. VAD alone cannot reliably distinguish backchannel from interruption; do not gate v0.1a on a capability v0.1a intentionally lacks.

### v0.1a numeric acceptance gates (hard pass/fail)

| Metric | Gate |
|---|---|
| `policy_replay_match_rate` | = 100% |
| `orphan_action_count` | = 0 |
| `assistant_audio_start_with_cause` | = 100% |
| `thinking_pause_false_positive_rate` | = 0 on fixture set [v0.1b-gated — see issue #10] |
| `direct_question_latency_p50` | < 800 ms |
| `direct_question_latency_p95` | < 1500 ms |
| `vad_detected_user_speech_to_stop_ms_p95` | < 200 ms |
| `physical_user_speech_onset_to_stop_ms_p95` | < 350 ms (tightens to <250 ms by v0.1b) |
| `false_interruption_count_per_10_min` | < 1 |

Both barge-in latencies must be gated separately. `physical_user_speech_onset_to_stop_ms` is what the user actually feels (product truth, loose at v0.1a because VAD-only adds detection lag). `vad_detected_user_speech_to_stop_ms` is the system-internal stop path — strictly gated because it measures the `AudioOutputController`.

> **v0.1a succeeds when the system can explain every utterance, replay every policy decision, stop when interrupted, wait through thinking pauses (v0.1b-gated — see issue #10), and answer direct questions promptly.**

---

## NEXT TASKS (ordered)

Each task is one PR. Each PR turns exactly one `pytest.skip` into a passing test, OR adds a stub for a downstream capability, OR is a docs update. Mixing is not allowed (see `CLAUDE.md`).

1. **Implement `schemas.py` from spec §Part 5.** Concrete dataclasses / enums for `Event`, `ReasonCode`, `DecisionTrace`, `TurnSignal`, `PolicyInputs`, `SpeakDecision`, `ThinkerProposal`, `MemoryItem`, `EvaluationCase`, `ReplayRun`, plus `SensitiveField`. No behavior, just types. Verify: schemas import cleanly; type checks pass.
2. **Implement `event_logger.py`** with async non-blocking discipline (invariant #10). On backpressure, emit `log_drop_or_degrade` rather than blocking the realtime path. Verify: a synthetic high-rate event stream does not block on a slow sink in a unit test.
3. **Implement `causal_graph.py`** — offline DAG reconstruction from `caused_by[]` edges. Verify: orphan detector unit test passes on a synthetic trace.
4. **Implement `AudioOutputController`** — playback lifecycle, stop-on-barge-in, generation cancel. Emits the Part 5 event_types (`assistant_generation_start`, `assistant_audio_buffer_queued/flushed`, `assistant_audio_stop_requested/completed`). Verify: unit test exercises the stop path with mocked TTS.
5. **Wire `VADDetector`** (`turn_detector_vad.py`) — Silero VAD per `docs/implementation-config.yaml`. Emits `TurnSignal` per §Part 5. Verify: detector emits on a recorded speech/silence sample.
6. **Implement `SpeakPolicy`** restricted to `{silence, full_response}`. Every `SpeakDecision` carries a `primary_reason_code` from `ReasonCode`. Verify: deterministic given recorded signals (Tier B replay precondition).
7. **Wire `ForegroundModel` adapter.** One adapter interface, one concrete instantiation behind it. Verify: adapter interface importable from `speak_policy.py` without leaking the SDK.
8. **Write fixture `thinking_pause_001`** (per spec §Part 6c). Audio + expected events. Verify: fixture loads in a pytest collection.
9. **Implement and pass `test_thinking_pause`.** First green test. Verify: `pytest -k thinking_pause` passes. [DEFERRED to v0.1b — see issue #10]
10. **Implement and pass `test_barge_in`.** Verify: VAD-to-stop p95 < 200ms on the fixture.
11. **Implement and pass `test_direct_question_latency`.** Verify: p50 < 800ms / p95 < 1500ms.
12. **Implement and pass `test_explicit_turn_handoff`.** Verify: companion responds promptly to "what do you think?".
13. **Implement and pass `test_false_interruption_rate`.** Verify: <1 false interruption per 10 minutes of scripted fillers.
14. **Implement and pass `test_policy_replay_exact`** (Stage 0 Tier B). Verify: 100% bit-identical replay on recorded signal traces.
15. **Implement and pass `test_decision_provenance`.** Verify: every `assistant_audio_start` has non-empty `caused_by[]`.
16. **Implement and pass `test_causal_graph_completeness`.** Verify: zero orphan actions on synthetic + recorded traces.
17. **First v0.1a ReplayRun report.** All 8 tests green; all 9 numeric gates met. Tag the repo `v0.1a`.

---

## v0.1b

Unlocks when v0.1a is green. Adds backchannel-aware EOU:

- `TurnDetectorSuite`: enable `SmartTurnDetector` OR a lightweight backchannel classifier.
- `SpeakPolicy`: add the `backchannel` action type.
- New required tests: `test_backchannel_survival`, `test_detector_ablation`.
- Acceptance: all v0.1a tests still pass + new tests pass. `physical_user_speech_onset_to_stop_ms_p95` tightens from <350 ms to <250 ms.

---

## Later stages (2-6) — deferred until v0.1 substrate passes

- **Stage 2 — Audio-video grounding.** `VisionSidecar` adapter, deictic detector, scene-change scoring, recent-visual-memory ring buffer. Contract tests cover deictic continuity, ambiguous-reference refusal, audio-visual conflict handling, temporal ordering, hallucination resistance. Eval datasets: ProactiveVideoQA (PAUC), EgoLifeQA.

- **Stage 3 — Speak / silence policy (full).** Expands the action set beyond `{silence, full_response}` to include `short_reaction`, `clarification`, `alert`, `tool_status`, `aesthetic_reaction`, `backchannel`. EOU thresholds remain isolated from speak-policy thresholds — they are tuned separately. Per-mode proactivity budgets are enforced.

- **Stage 4 — Memory.** Four-store schema (`session_state`, `core_user_profile`, `episodic_memory`, `semantic_relational`) with provenance per item. `SleepTimeAgent` runs async to mutate `companion_state`; foreground never blocks on memory ops. `forget` (historical correction) and `delete` (privacy operation) are distinct commands. Eval: LoCoMo, LongMemEval, MemoryAgentBench.

- **Stage 5 — Background reasoning & tool routing.** Two-tier MCP (fast deterministic path + smart background-reasoner path). Foreground may only narrate tool progress when a `ToolProgressEvent` exists; filler budget caps even truthful narration. Tool calls cancel within 300ms on barge-in.

- **Stage 6 — Companion texture.** `ThinkerProposalGen` emits proposals only and cannot speak directly — all proposals pass through `SpeakPolicy`. `aesthetic_reaction` action type respects the rubric: short (<=8 words), grounded in available sensors, non-possessive, non-diagnostic, non-flattering. Per-mode proactivity budgets; attachment-risk monitor active. The texture is built on top of a working substrate, not in place of it. Eval: 1-week human RCT (primary); EQ-Bench 3, PersonaMem (regression).

---

## Decision log

Add entries as ReplayRuns produce them. Each entry: date, what changed, why, which test regressed/passed.
