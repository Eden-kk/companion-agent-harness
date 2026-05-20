# Execution plan: turn-free continuous companion

**Status:** DRAFT (round 0) — for plan-critic review.
**Design source:** [`design-turn-free-continuous-companion.md`](design-turn-free-continuous-companion.md) (converged, READY; probes §3.1/§3.2 = GO).
**Discipline:** one PR, one verifiable success criterion (CLAUDE.md rule 4). Stages ship behind a `--continuous` flag, default OFF; the existing turn-based orchestrator is untouched until the new core passes (strangler-fig, design §10.5).

This plan turns design §10 stages 2–6 into PRs. Stages 1/1b/1c/1d are completed probes (design §3.1/§3.2), not implementation work.

---

## Build approach (strangler-fig)

A **new** `companion_harness/continuous_orchestrator.py` is written alongside `realtime_orchestrator.py`, consuming the same adapters + EventLogger below it. No retrofit of the 2610-line turn-based orchestrator. A/B both on the same fixtures. The turn machinery + Path A/B variants are retired only after PR5 passes, gated by a test-migration table (see §Test-migration gate).

What is reused vs rewritten is fixed by design §10.5; this plan does not relitigate it.

---

## PR1 — Continuous feeder + sliding window (design Stage 2)

**Scope.** New `continuous_orchestrator.py` with a continuous audio feeder that pumps chunks indefinitely (replaces the per-response drain). Enable `sliding_window_mode="context"` on the duplex model. Per-chunk events logged; "turns" become an audit-time segmentation, not a runtime unit.

**Precondition (design §10 Stage 2 / round-2 finding).** Characterize audio-encoder KV mid-generation reset (~1500 cap): run a >15-min session that forces ≥1 reset event; assert no coherence regression across the reset (measure: output-text coherence + backbone attention sanity before vs after). Ship a probe `scripts/probe_audio_kv_reset.py`; record the result in this plan before merging PR1.

**Files.**
- `companion_harness/continuous_orchestrator.py` (new)
- `companion_harness/foreground_model_minicpm.py` — set `sliding_window_mode="context"` via a duplex-param kwarg; no behavior change to the turn-based path
- `manual_test_console/live_pipeline.py` — `--continuous` opt-in kwarg (default False)

**Success criterion.** `test_continuous_feeder_emits_per_chunk_events`: under `SyntheticClock` + `DirectAudioInputFeeder`, a fixture WAV runs through `continuous_orchestrator` end-to-end and emits per-chunk events with closed `caused_by[]` (invariant #1); the turn-based suite is unchanged (regression-green). Plus the audio-KV-reset precondition probe shows no regression.

**Invariants touched.** #1 (per-chunk logging), #10 (feeder uses non-blocking `EventLogger.log`).

---

## PR2 — Per-chunk PolicyInputs schema + continuous gate scaffold (design Stage 4, schema half)

**Scope.** Define `PerChunkPolicyInputs` (design §7 "Per-chunk PolicyInputs" block) as a frozen dataclass. The continuous per-chunk SpeakPolicy gate reuses the rule *structure*, `ReasonCode` taxonomy, and `DecisionTrace` format from `speak_policy.py`; it does **not** reuse the per-turn input schema or thresholds.

**Carried-forward finding (round-3 concern 1) — deterministic serialization.** `cooldown_state` and `proactivity_budget_remaining` must NOT be bare dicts in the replay signal. Flatten to scalar fields with a fixed key order, e.g.:
- `cooldown_aesthetic_ms: int`, `cooldown_question_ms: int`, … (one per cooldown bucket)
- `budget_aesthetic_remaining: int`, `budget_question_remaining: int`, … (one per budget bucket)
Bit-identical Tier-B replay requires a canonical, scalar, ordered serialization — no dict iteration-order dependence.

**Files.**
- `companion_harness/schemas.py` — `PerChunkPolicyInputs` dataclass
- `companion_harness/continuous_speak_policy.py` (new) — `decide_chunk(inputs) -> SpeakDecision`, pure + deterministic
- `tests/test_continuous_policy_replay.py`

**Success criterion.** `test_policy_replay_exact_continuous`: a recorded sequence of `PerChunkPolicyInputs` replayed twice through `decide_chunk` yields **bit-identical** `SpeakDecision` + `DecisionTrace` sequences (invariant #5). A determinism guard asserts no `dict`/`set` iteration leaks into the decision path.

**Invariants touched.** #5 (Tier-B determinism), #2/#4 (gate is the only path to speech).

---

## PR3 — Model-native barge-in primary + BackchannelClassifier veto (design Stage 3)

**Scope.** Relax the `current_turn_ended` gate (the change validated by probes §3.1/§3.2) in the continuous path only. Wire the model's per-chunk `is_listen` as the **primary** barge-in signal; VAD/SmartTurn demote to safety net (fire only if the model is too slow). `BackchannelClassifier` runs as a **confirmatory veto**: if the model yields but the classifier scores overlapping audio as a backchannel, suppress the yield.

**Flag-flip precondition (design §6 / §10 Stage 3).** Re-probe at **N≥20 with real human audio** (not synthetic). **GO criterion: interruption yield-rate ≥ 80% AND backchannel false-yield-rate ≤ 10%.** Only after this passes does the BackchannelClassifier demote from veto to logging-only and VAD demote to safety net. Until then both stay in the loop.

**Files.**
- `companion_harness/continuous_orchestrator.py` — barge-in routing (model-primary), BC veto
- `companion_harness/foreground_model_minicpm.py` — gate-relax confined to a `continuous=True` code path; turn-based path keeps the gate
- `scripts/probe_barge_in_real_audio.py` (the N≥20 re-probe harness)
- `tests/test_continuous_barge_in.py`

**Success criterion.** `test_continuous_barge_in_and_backchannel`: on fixtures, an interruption yields within the barge-in latency budget and a backchannel does not stop the model; the BC-veto path is exercised and logged. The N≥20 real-audio re-probe is a documented merge precondition for the demotion sub-change (not the whole PR).

**Invariants touched.** #8 (silence wins ties — model yield gated by policy), barge-in latency budgets (Stage 1).

---

## PR4 — Background-thought injection + role-typed KV (design Stage 5)

**Scope.** Inject background-reasoner output as role-tagged `background-think` text units via `streaming_prefill(text_list=...)` at chunk boundaries (design §4.1 level-2 in-context protocol — no finetune, per probe 1c). Role-typed units (Strategy 1) + fuller role snapshot (Strategy 3). **Strategy 2 (role-aware eviction) is deferred** — not built until vanilla `context` eviction shows a concrete coherence failure.

**Think-source (settled, probe 1d).** Injected background model, NOT a native foreground think-channel.

**Background-reasoner execution mechanism.** Separate process on a **dedicated CUDA stream** (design default). 

**Carried-forward finding (round-3 concern 2) — GPU-contention gate.** Stage-5 prerequisite with a numeric bound: **foreground per-chunk latency p95 < 250 ms under concurrent background-model load** on b200 (tighten toward the ~200 ms chunk target in PR5). Measure before enabling the background model in any default-on path.

**Incorporation re-probe (design §11 / round-2 finding).** Before the background model goes to production: **≥ 80% incorporation at N≥10**. Below that, treat as an injection/turn-handling bug first; finetune only if unfixable.

**Files.**
- `companion_harness/continuous_orchestrator.py` — scratchpad injection hook
- `companion_harness/background_reasoner.py` — continuous mode, separate process + CUDA stream
- `companion_harness/foreground_model_minicpm.py` — `inject_scratchpad(text, caused_by)` + role-tagged units + full snapshot upgrade
- `tests/test_background_think_injection.py`

**Success criterion.** `test_background_think_compliance`: an injected `[CONTEXT:…]` unit is not voiced by the foreground (the don't-voice property, invariant-critical), and the injection is a logged event with closed `caused_by[]`. GPU-contention measurement recorded under the p95 bound; incorporation re-probe recorded ≥80% before any default-on.

**Invariants touched.** #1, #2/#4 (injected context is data, gate is on output tokens), #9 (tool-progress narration still evidence-bound), #10.

---

## PR5 — Tuning + default-on (design Stage 6)

**Scope.** Tune `chunk_ms` → ~200 ms and `listen_prob_scale` (production value biases toward listening — invariant #8). Manual-test cycle. Flip `--continuous` default ON only after a clean cycle + green gates.

**Precedence rule (design §9).** Policy gate is authoritative; `listen_prob_scale` only biases the model's upstream `is_listen` sampling; gate-closed ⇒ silence regardless of model preference. Encode this ordering in the gate.

**Success criterion.** `test_continuous_latency_and_proactivity`: with `chunk_ms≈200`, barge-in p95 within the Stage-1 budget and Stage-6 `false_proactive_utterances_per_hour` within target on the fixture set; manual-test findings doc closed with zero critical open items.

**Invariants touched.** #8, Stage-1 latency budgets, Stage-6 texture gates.

---

## Test-migration gate (design §10.5)

Before the turn machinery + Path A/B variants are removed, produce a **test-migration table**: each existing contract test → `kept-as-is` / `ported-to-continuous-core` / `deleted-with-rationale`. No turn-based code is deleted until every test is accounted for and the continuous-core ports are green. Tracks: `tests/test_path_b_phase_a.py`, `tests/test_streaming_speculative_continuous.py`, and the orchestrator/barge-in/policy suites.

---

## Dependency graph

```
PR1 (feeder + window)
   └─> PR2 (per-chunk PolicyInputs + gate)   ── deterministic Tier-B contract
          └─> PR3 (model-native barge-in + BC veto)  ── needs the gate
                 └─> PR4 (background-think injection)  ── needs continuous loop + gate
                        └─> PR5 (tuning + default-on)  ── needs all of the above
Test-migration gate: blocks deletion of turn machinery (after PR5 green)
```

PR1→PR2→PR3→PR4→PR5 is strictly sequential (each builds on the prior's contract). The two real-audio/GPU re-probes (PR3 flag-flip, PR4 GPU+incorporation) gate *demotion/default-on sub-changes*, not the PR merges themselves — so dev can proceed while the human-in-the-loop probes run.

## Rollout & reversibility

Every stage is flag-gated (`--continuous` default OFF). Reverting = flip the flag; behavior returns to the turn-based orchestrator. Default-on is earliest v0.3, after PR5 + a clean manual-test cycle. The turn-based path remains the fallback until the test-migration gate is satisfied.

## Open items carried from design review (must be resolved in-PR, not deferred again)

| Item | PR | Resolution |
|---|---|---|
| Flatten `cooldown_state` / `proactivity_budget_remaining` to scalar ordered fields | PR2 | deterministic Tier-B serialization |
| Numeric GPU-contention gate (foreground p95 < 250 ms under background load) | PR4 | measured on b200 before default-on |
| N≥20 real-audio barge-in/backchannel re-probe (≥80% / ≤10%) | PR3 | gates VAD/BC demotion |
| ≥80%@N≥10 incorporation re-probe | PR4 | gates background-model production |
| Audio-KV mid-generation reset characterization | PR1 | precondition probe |
| arxiv 2605.12460 citation verification | PR-any | verify PDF or drop before citing in shipped code/docs |
