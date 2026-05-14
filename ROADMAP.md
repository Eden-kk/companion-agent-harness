# Roadmap

## SHIPPED MILESTONES

### v0.1a — COMPLETE (tagged `v0.1a`)

The v0.1a MVP was the **VAD-only baseline**: every utterance has a cause, every policy decision is replayable, barge-in stops the assistant, and direct questions are answered promptly. All 8 required contract tests passed; all 9 numeric gates met. Stage 0 (causal graph + replay privacy) and Stage 1 (VAD-only) were enabled.

### v0.1b — COMPLETE (tagged `v0.1b`)

Added backchannel-aware EOU: `SmartTurnDetector` + `BackchannelClassifier` (both required per spec amendment in issue #20), the `backchannel` action type in `SpeakPolicy`, and the `test_thinking_pause`, `test_backchannel_survival`, `test_detector_ablation` contract tests. All v0.1a tests still pass. 45 tests green in the canonical venv (`/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/ -q`). Stages 0–1 contract tests green. Full task breakdown: `docs/roadmap-v0.1b-draft.md`.

---

## CURRENT MILESTONE: v0.1c (DRAFT — awaiting project-lead sign-off)

Stage 2 — **Audio-video grounding.** Full task breakdown and open questions: [`docs/roadmap-v0.1c-draft.md`](docs/roadmap-v0.1c-draft.md) (currently DRAFT).

### v0.1c pinned success criterion

> **v0.1c succeeds when the system can ground a deictic reference ("what is this?" / "what about that one?") to the correct frame in the `raw_video` causal chain, refuse to guess when the reference is ambiguous or the camera is blocked, surface audio-visual conflicts instead of papering over them, and order recent visual events correctly — while every utterance stays explainable, every policy decision bit-identically replayable, and the deictic detector independently ablatable. Whether the visual answer content is correct is measured on b200 against ProactiveVideoQA / EgoLifeQA and reported as advisory only — it is not a release gate.**

### Scope delta from v0.1b

- `VisionSidecar`: enable the vision path, adapter-first (`raw_video` frame ingest, recent-visual-memory ring buffer, scene-change scoring).
- New adapter — `DeicticDetector` (`deictic_detector.py`): gates the explicit grounding pass; adapter-first behind a `DeicticModel` Protocol.
- `ForegroundModel` adapter: extend `DuplexModel` Protocol for optional `video_frame` input.
- `SpeakPolicy`: new `ReasonCode` members (`DEICTIC_AMBIGUOUS`, `VISUAL_LOW_CONFIDENCE`, `AUDIO_VISUAL_CONFLICT`); `clarification` wiring TBD per open question 1.
- Stages: 0 + 1 carried (regression gates); **Stage 2 enabled**. Stages 4 / 5 / 6 remain disabled.

### New contract tests (7)

`test_current_frame_grounding`, `test_deictic_continuity`, `test_recent_visual_memory`, `test_hallucination_resistance`, `test_ambiguous_deictic_refusal`, `test_audio_visual_conflict`, `test_temporal_event_order`

See `docs/roadmap-v0.1c-draft.md` §NEXT TASKS for the full ordered task list (20 tasks).

---

## PARALLEL TRACK: Live-Loop Integration (EXTRA-SPEC — DRAFT, awaiting sign-off)

A product-integration track that runs alongside the spec milestones. Goal: a human can speak to the harness through a live microphone and hear a synthesized voice reply — every spoken utterance policy-approved, the full path `caused_by[]`-closed and replayable, barge-in within the spec latency budget, `EventLogger` non-blocking on the realtime path. This is **not a spec stage** and does not modify the frozen architecture. Full plan: [`docs/milestone-live-loop-integration-draft.md`](docs/milestone-live-loop-integration-draft.md).

Can run **fully parallel to v0.1c** — no dependency on v0.1c outputs. One coordination point: the `DuplexModel` Protocol extension must be sequenced between the two tracks (one owns the change, the other consumes it).

---

## Later stages (3-6) — deferred until v0.1c substrate passes

- **Stage 2 — Audio-video grounding.** Active in v0.1c (see above).

- **Stage 3 — Speak / silence policy (full).** Expands the action set beyond `{silence, full_response}` to include `short_reaction`, `clarification`, `alert`, `tool_status`, `aesthetic_reaction`, `backchannel`. EOU thresholds remain isolated from speak-policy thresholds — they are tuned separately. Per-mode proactivity budgets are enforced.

- **Stage 4 — Memory.** Four-store schema (`session_state`, `core_user_profile`, `episodic_memory`, `semantic_relational`) with provenance per item. `SleepTimeAgent` runs async to mutate `companion_state`; foreground never blocks on memory ops. `forget` (historical correction) and `delete` (privacy operation) are distinct commands. Eval: LoCoMo, LongMemEval, MemoryAgentBench.

- **Stage 5 — Background reasoning & tool routing.** Two-tier MCP (fast deterministic path + smart background-reasoner path). Foreground may only narrate tool progress when a `ToolProgressEvent` exists; filler budget caps even truthful narration. Tool calls cancel within 300ms on barge-in.

- **Stage 6 — Companion texture.** `ThinkerProposalGen` emits proposals only and cannot speak directly — all proposals pass through `SpeakPolicy`. `aesthetic_reaction` action type respects the rubric: short (<=8 words), grounded in available sensors, non-possessive, non-diagnostic, non-flattering. Per-mode proactivity budgets; attachment-risk monitor active. The texture is built on top of a working substrate, not in place of it. Eval: 1-week human RCT (primary); EQ-Bench 3, PersonaMem (regression).

---

## Decision log

Add entries as ReplayRuns produce them. Each entry: date, what changed, why, which test regressed/passed.
