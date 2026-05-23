# Execution plan — TACT-Bench MiniCPM adapter on the generalized eval framework

**Status:** draft for review
**Date:** 2026-05-22
**Branch:** `feat/tact-bench-vanilla-vs-prompted` (harness) — the MiniCPM-specific layer
**Base:** `origin/main` @ f3af805 — the generalized eval framework

## 0. Goal

Promote the standalone probe scripts (`scripts/probe_tact_vanilla_vs_prompted.py`,
`scripts/probe_tact_pending_injection.py`) into a first-class **benchmark adapter**
on the generalized six-protocol eval subsystem, so TACT-Bench (held-result delivery:
*when an always-on agent should stay silent*) runs through
`python -m companion_harness.evals run --adapter tact_bench` and emits the standard
replayable artifact tree.

This is the **Phase B** counterpart to the standalone probes: same logic, on the
generalized seams, with the analysis-driven metric/scenario fixes folded in.

## 1. The generalized / model-specific boundary

The whole point of this branch: **only the driver is MiniCPM-specific.** Everything
else is model-agnostic and either already on `main` or is benchmark-definition data
in the `tact-bench` repo.

| Layer | Where it lives | Model-coupled? |
|---|---|---|
| Six protocols, `BenchmarkAdapter` (`evals/protocols.py`) | main | no |
| `EvaluationCase` / `ReplayRun` (`companion_harness/schemas.py`) | main | no |
| Runner CLI (`evals/runners.py`), registry (`evals/registry.py`) | main | no |
| `SyntheticClock`, reporters (`evals/scenarios/`, `evals/reporters/`) | main | no |
| Adapter template (`evals/adapters/full_duplex_bench.py`) | main | no |
| Scenario set + ground truth + prompts (`scenarios.yaml`) | **tact-bench repo** | no |
| `CaseSource` (scenarios.yaml → `EvaluationCase`) | this branch (`evals/adapters/tact_bench.py`) | no |
| Delivery **judge** (Examiner) | this branch | no |
| Metric classes | this branch | no |
| **`TactMiniCPMDriver`** (`ScenarioDriver`) | **this branch** | **YES — the only coupled piece** |

**Consequence (the generalization claim):** swapping in a non-MiniCPM model = writing
a new `ScenarioDriver` against the same seam; `CaseSource` / judge / metrics / reporter
are untouched. Keep the driver the *only* place that imports the model SDK
(CLAUDE.md "adapter-first").

## 2. What we already learned (folds into the design)

From the standalone probe runs (committed under `tact-bench/.../results/`):

1. **Audio input is an echo confound.** Fed TTS user speech, the duplex model
   transcribes/parrots it. Text input on a 1s silence clock removes this. → The
   adapter's default driver mode is **text/silence-clock**; audio is a secondary mode.
2. **The ability is latent but not promptable.** 12-scenario text run: prompting only
   improved the false-alarm axis (cried-wolf 0.00 vs 1.00) and otherwise tied/regressed
   (urgent-miss 0.67/0.67, form 0.17 vs 0.33). → Keep vanilla & prompted as two
   `run_config` arms.
3. **The form metric was conflated with delivery rate.** Conditioned on a valid
   delivery, both arms were 100% brief. → **Split** form into `delivery_rate` +
   `conditional_form_accuracy` (PR4).
4. **The deferral/timing axis never fired** — no DEFER case produced any delivery, so
   breakpoint-hit was unmeasurable even with a working clock. → Add a **Mode-A
   structured `NOW/WAIT/DROP` probe** to test whether deferral is *elicitable* vs
   *absent* (PR6), and report breakpoint-hit as `n/a (no deferred delivery)` honestly.
5. **Framing nits already fixed** (REVISIONS R1 tag-vocalization, R2 URGENT dedup).

## 3. Target adapter shape (`companion_harness/evals/adapters/tact_bench.py`)

```
build_tact_bench(input_mode="text", arm="prompted", judge="openai") -> BenchmarkAdapter
  case_source     = TactCaseSource          # scenarios.yaml -> EvaluationCase
  scenario_driver = TactMiniCPMDriver        # duplex silence-clock / audio  [MODEL-SPECIFIC]
  examiner        = DeliveryJudge | None     # OpenAI Mode-B delivery labels
  metrics         = [CriedWolf, UrgentMiss, BreakpointHit, DeliveryRate, ConditionalForm]
  failure_slicer  = TactFailureSliceExtractor
  reporters       = [json_reporter, md_reporter]   # reuse main's reporters
```

`EvaluationCase` mapping (reuse the optional benchmark fields):
- `inputs` = `{user_script, item, input_mode}`
- `expected_behavior` = `{behavior, exposes, t_available, becomes_stale_at, ground_truth}`
- `consent_class` = `"safe_eval_fixture"`; free-text `payload`/`user_script` wrapped in
  `SensitiveField` before they touch any audited store (CLAUDE.md).

`ReplayRun.results` carries the per-chunk trajectory + the user-speaking mask so metrics
are pure functions of the run.

## 4. PR slices (CLAUDE.md rule 4 — one outcome each; each turns a `pytest.skip` green)

**PR1 — `TactCaseSource` (model-agnostic).**
Load `scenarios.yaml` → typed `EvaluationCase`s; carry `exposes`/behavior/timing into
`expected_behavior`.
*Success:* `test_tact_case_source` loads all 12 cases, 3 per behavior, with `exposes`
preserved. No model import.

**PR2 — `TactMiniCPMDriver` (text/silence-clock) [MODEL-SPECIFIC].**
Port `_run_arm_text` behind `ScenarioDriver.run()`. Drive `MiniCPMStreamingModel` with
1s silence per tick (the clock), user turns + held result via `streaming_prefill(text_list=)`
as a ChatML system turn. Emit through `EventLogger` (async, non-blocking — invariant #10):
`benchmark_case_started/completed`, `fixture_audio_chunk_injected`,
`native_duplex_invocation`, and a **new** `held_result_injected` event. Every Event
carries `caused_by[]` (invariant: DAG closes).
*Success (CI gate):* `tests/test_tact_driver.py` with a **fake duplex** (no GPU)
yields a ReplayRun whose event log has started/held_result_injected/completed (closed
`caused_by` DAG) and whose `results.trajectory` flags injection at `t_available`. A
real `--limit 1` GPU smoke is run manually on b200, not in CI (plan-critic C4).
*Sub-task:* register `held_result_injected` in `EVAL_EVENT_TYPE_SCHEMAS`
(`evals/schemas.py`). NOTE (plan-critic B2): the eval retention ids
(`eval_run_30d`, `raw_media_default_300s`) are **already** referenced by the existing
eval schemas and are absent from `replay_privacy_policy.yaml`; no test enforces eval-id
membership. This is a pre-existing gap — `held_result_injected` reuses `eval_run_30d`
for consistency; fixing the shared yaml is out of scope for this branch.
*Resolved (plan-critic B3):* `streaming_prefill(text_list=...)` is verified in the model
source (modeling_minicpmo.py L2759/L3094) and at runtime by
`scripts/probe_tact_pending_injection.py` + the text-mode runs — not an open assumption.
*Mechanism:* emit via the async, non-blocking `EventLogger` (invariant #10), collected
through an async sink and serialized to the jsonl — per this plan (not the `event_sink`
shortcut some other drivers use).

**PR3 — `DeliveryJudge` Examiner (model-agnostic).**
Port the OpenAI Mode-B judge behind the `Examiner` seam; persist `judge-labels.json`;
skip cleanly without `OPENAI_API_KEY` (metrics → PENDING). Cache verdicts by
(case_id, arm, trajectory-hash) for determinism.
*Success:* `test_delivery_judge` labels a fixture trajectory from a recorded cassette
(no network in CI).

**PR4 — Metric classes (model-agnostic) + the form split.**
`CriedWolf`, `UrgentMiss`, `BreakpointHit`, **`DeliveryRate`**, **`ConditionalForm`**
implementing `Metric.compute(replay_run) -> MetricValue`. Port the scoring math from
`probe_tact_vanilla_vs_prompted.py::_compute_metrics`, citing it; keep the
`first_delivery_chunk >= t_available` phantom guard.
*Success:* `test_tact_metrics` on a fixture run reproduces hand-computed values,
incl. `conditional_form == 1.0` where unconditional form < 1.0.

**PR5 — Registry + runners wiring (model-agnostic).**
Add `tact_bench` to `ADAPTERS`; wire `_run_tact` in `runners.py`; reuse json+md reporters.
NOTE (plan-critic C5): `runners.py` has no `--input-mode`/`--arm`/`--judge` args today —
PR5 must add them to the argparse, else the success command errors on an unknown flag.
*Success:* `python -m companion_harness.evals run --adapter tact_bench --input-mode text`
writes `run.json` / `metrics.json` / `report.md` / `event_logs/`.

**PR6 — Analysis-driven coverage (benchmark-definition + driver).**
(a) `scenarios.yaml`: add a **FULL-correct** case (complex result; brief under-informs)
and a **verbose-trap** case so `ConditionalForm` actually discriminates, not just rewards
brevity. (b) Add **Mode-A** structured `NOW/WAIT/DROP` probe to the driver to test
whether deferral is elicitable.
*Success:* `ConditionalForm` separates a brief-correct from a verbose-wrong case;
Mode-A emits per-decision-point labels on the DEFER cases.

**PR7 (optional) — audio-mode parity + cross-check.**
`TactMiniCPMDriver(input_mode="audio")` parity; `docs/results-tact-crosscheck.md`
comparing text vs audio and probe-vs-adapter numbers within tolerance.
*Success:* adapter text-mode metrics match the standalone probe within stated tolerance.

## 5. Invariants & compliance (non-negotiable, from CLAUDE.md)

- **EventLogger async, non-blocking** on the run path; backpressure → `log_drop_or_degrade`.
- **Every Event carries `caused_by[]`**; orphan events fail Stage 0.
- **Free-text** (`payload`, `user_script`, judge rationale) via `SensitiveField`.
- **Adapter-first:** only `TactMiniCPMDriver` imports the model SDK; metrics/judge/casesource
  must not.
- **Determinism:** metrics are pure functions of `ReplayRun`; judge verdicts cached.

## 6. Risks / open questions

- **R1 — deferral may be genuinely absent**, not just un-elicited; PR6 Mode-A
  disambiguates "can't defer" from "won't defer under emergent gating."
- **R2 — judge cost/nondeterminism**; mitigate with cassettes in CI + verdict cache.
- **R3 — `held_result_injected` retention policy** must exist before PR2 emits it.
- **OQ1 — RESOLVED (PR1):** the scenario definition is **vendored** into the harness at
  `companion_harness/evals/adapters/tact_bench_data/scenarios.yaml` so the adapter runs
  CI-hermetically; the tact-bench repo remains the upstream author (keep in sync).
  `CaseSource` accepts a `scenarios_path` override for non-default sets.
- **OQ2 — keep the standalone probes** as a smoke-test, or delete after PR5 supersedes
  them? Recommend: keep `probe_tact_pending_injection.py` (feasibility gate), retire the
  runner probe once the adapter reaches parity (PR7).

## 7. Minimal first step

PR1 (`TactCaseSource`) — pure, model-free, unblocks everything downstream and is
verifiable with a fixture-only test (no GPU, no network). Confirm OQ1 (definition path)
before writing it.
