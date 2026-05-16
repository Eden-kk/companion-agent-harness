# Plan — v0.2c Execution (Real benchmark data loaders: CANDOR + FullDuplexBench)

## Status: **DRAFT** — drafted 2026-05-16. Source roadmap: `docs/roadmap-v0.2-draft.md` §Wave 3 (Tasks 12–14). Not yet plan-critic'd; not yet sub-agent dispatched.

> v0.2c flips Wave 3 of the v0.2 roadmap from "synthetic-only adapters" to "real HuggingFace datasets streaming, opt-in via `--mode real`." The deliverable is three real-mode loaders (CANDOR, FullDuplexBench V1, V1.5) that share the existing `CaseSource` Protocol and produce `EvaluationCase` records the existing scenario drivers can run. Synthetic mode remains the default for every adapter; `--mode real` is the explicit opt-in. No new abstractions: the runner dispatches via inline `if/elif` on adapter name (3 branches; a registry abstraction is not justified). Audio bytes travel inside `EvaluationCase.inputs` as `{'audio_pcm_bytes': bytes, 'sample_rate': int}` — no new schema field. HuggingFace auth (`HF_TOKEN` + accepted gates for CANDOR + FDB) is a hard prerequisite, not a soft risk.

---

## §0 Anchors (load-bearing decisions; every task must respect these)

1. **`datasets` library only.** No custom HTTP downloader, no `huggingface_hub` snapshot calls, no S3 SDK. The single dependency is `datasets>=2.16` (already in `pyproject.toml [project.optional-dependencies] eval`).
2. **`streaming=True` default.** Every `load_dataset(...)` call streams. No full-corpus materialization to disk in Wave 3. Smoke runs (`--limit 200` or below) MUST never download more than the rows consumed.
3. **Per-adapter schema mapper.** Each loader owns a `_<name>_row_to_evaluation_case(row)` function. No "generic adapter-agnostic mapper" — three call sites do not justify shared abstraction (CLAUDE.md coding rule 2).
4. **Real-mode opt-in via `--mode real`.** Default remains `--mode synthetic`. `--mode real` flips `CandorCaseSource(synthetic=False)` and the new FDB `real=True` flag, then runs the loader path.
5. **Audio goes in `EvaluationCase.inputs`.** The schema (`companion_harness/schemas.py:300`) has `inputs: dict | None`. Real-mode CANDOR rows produce `inputs = {'audio_pcm_bytes': <bytes>, 'sample_rate': <int>, ...timing fields...}`. **DO NOT add a new `audio_bytes` field to `EvaluationCase`.** That would be a schema change touching the spec-frozen 8 positional fields region and is out of scope.
6. **HF auth as hard prerequisite.** Before T1 begins, `huggingface-cli whoami` must succeed AND the dev account must have accepted the CANDOR + FDB V1/V1.5 gates. Loader init does the same check at runtime and fails loudly with an actionable error if `HF_TOKEN` is missing or the user has not accepted the gate. The check is **not** retry-on-failure; it is fail-fast.

---

## §0a Open-question resolutions (carried from roadmap → plan)

These OQs were left ambiguous in `docs/roadmap-v0.2-draft.md` §Wave 3 and are decided here for v0.2c:

| OQ | Resolution in v0.2c |
|---|---|
| Dataset `revision=` pinning | Every `load_dataset()` call pins `revision=<commit_hash>` in the loader docstring + code. If the upstream dataset does not pin row ordering at a given revision, the loader applies a deterministic `sort_by_stable_key(row)` fallback inside the streaming generator (sort by `(speaker_id, start_time)` for CANDOR; by `(scenario, case_id)` for FDB) so `real_mode_eval_score_replay_match_rate == 1.0` (roadmap gate). |
| License / auth posture | Treated as a **hard prerequisite block**, not a risk row. Pre-flight (§Pre-flight) gates the entire Wave; if any of the three datasets is ungated or revoked for the dev account, **defer Wave 2 dispatch to a follow-on PR**. Loader docstring records the pinned license string; loader init refuses if upstream license string changes. |
| `--filter K=V` | **Removed** for v0.2c. Only `--limit N` ships. The roadmap mentioned `--filter K=V` (Task 14); v0.2c defers it because (a) every real-mode smoke fixture uses `--limit 5`, (b) downstream slicing is the per-adapter mapper's job, (c) `K=V` parsing is a CLI surface and stub registry we don't need yet. If a use case appears, file a follow-on. |
| `consent_class` for real-mode rows | Reuse the existing `"safe_eval_fixture"` constant. New vocabulary (`"licensed_corpus_research_only"`, etc.) is deferred to a follow-on schema PR. Rationale: every Wave 3 dataset is already-published research data; the consent class distinction is unused downstream in v0.2c and would force a schema-test update for zero benefit. |
| Synthetic-mode `audio_pcm_bytes` | Stays absent. Synthetic CANDOR/FDB cases carry only timing fields and `synthetic: True` in `inputs`. Only real-mode rows populate `audio_pcm_bytes`. |

---

## §Pre-flight verification block (MUST PASS before Wave 2 dispatch)

This block is non-optional. It runs at the start of T0a (the dispatch agent reads it first). If any check fails, the named task is conditional and the plan branches as documented.

1. **Verify `full_duplex_bench.py` `real`/`synthetic` seam state.** Current main: `FullDuplexBenchV1CaseSource` and `FullDuplexBenchV15CaseSource` (`companion_harness/evals/adapters/full_duplex_bench.py:88,110`) have **NO `synthetic` / `real` flag and NO `NotImplementedError` raise**. Both unconditionally call `_make_synthetic_cases` in `__post_init__`. **Conclusion: seam is missing.** ⇒ T0a is required (not optional).
2. **Locate actual HF dataset slugs for CANDOR + FDB V1/V1.5.** This requires HF Hub inspection at T0a impl time. The plan does NOT assume specific slugs. The T1 coder agent does:
   - `huggingface-cli search candor` and `huggingface-cli search "full duplex bench"`, OR
   - inspect the CANDOR paper / FDB paper for the canonical HF repo identifier.
   - If a canonical slug cannot be located, **defer T1 (CANDOR) and/or T3 (FDB) to a follow-on PR** and ship the rest of Wave 3 (the seam + CLI surface) without the real loader path active. Document the gap in the PR description.
3. **Verify `huggingface-cli whoami` succeeds with credentials for both gated datasets.** Run on the b200 dev machine where the loader will execute. If `whoami` fails OR if the dev account has not accepted the gate for one of the three datasets, **block T1/T3 entirely** and surface a single "auth setup" task ahead of the rest. Do not write loader code that will 401 at first call.
4. **Verify `CandorScenarioDriver` handles audio rows.** Current main: `CandorScenarioDriver.run()` (`companion_harness/evals/adapters/candor.py:154`) reads only `turn_gap_ms`, `overlap_ms`, `backchannel_pause_ms`, `response_delay_ms` from `case.inputs`. It does **not** read `audio_pcm_bytes`. ⇒ T2 is required: extend the driver to surface real-audio rows into the existing distributional metrics flow (or document explicitly why audio bytes are ignored in this milestone). The default expectation: real-mode CANDOR rows still emit the four timing observations (derived from the row's transcript / VAD), so the driver's existing aggregation contract holds. Audio bytes are carried for downstream Phase C examiner use; v0.2c does not consume them in the driver.

**Pre-flight outcome matrix:**

| Outcome | Wave 2 dispatch | Tasks in this PR |
|---|---|---|
| All 4 checks pass | Full Wave 2 dispatch | T0a, T0b, T1, T2, T3, T4, T5, T6a, T6b, T6c |
| Check #2 fails for CANDOR only | Partial: ship FDB path | T0a, T0b, T3, T4, T5 (FDB-only branches), T6b, T6c |
| Check #2 fails for FDB only | Partial: ship CANDOR path | T0a (FDB seam still adds NotImplementedError), T0b, T1, T2, T5, T6a, T6c |
| Check #2 fails for both | Ship seam + CLI only | T0a, T0b, T5 (`--mode real` rejects all three with actionable error), T6c |
| Check #3 fails | Block entire Wave 3 | Single "HF auth setup" task; reschedule v0.2c |
| Check #4 reveals need for driver redesign | T2 expands; defer T1 within same PR | T0a, T0b, T2 (driver work), T6c — T1 deferred |

---

## §Dependency graph

```
┌─────────────────────────────────────────────────────────────────────┐
│ Pre-flight (manual at dispatch time; outcome matrix above)         │
│   PF1: FDB seam state — confirmed MISSING ⇒ T0a is required       │
│   PF2: HF slug locatable for each of {CANDOR, FDB v1, FDB v1.5}   │
│   PF3: `huggingface-cli whoami` + gate acceptance                  │
│   PF4: CandorScenarioDriver audio handling                         │
└─────────┬───────────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────────┐
│ Wave 1 (seams + dispatch — must land before any loader work)       │
│   T0a: Add real/synthetic flag + NotImplementedError to FDB v1/v1.5│
│   T0b: Extend runners.py to dispatch candor + fdb_v1 + fdb_v1_5    │
└─────────┬───────────────────────────────────────────────────────────┘
          │
   ┌──────┴──────┐
   ▼             ▼
Wave 2A         Wave 2B
(CANDOR)        (FDB)
T1 loader       T3 loader (V1 + V1.5 inline; no shared helper)
T2 driver       T4 mappers (per-version, inline)
T6a tests       T6b tests
          │
          ▼
Wave 3 (CLI surface)
   T5: --mode {synthetic,real} + --limit N
   T6c: CLI tests
```

---

## §Concurrency map

| Wave | Concurrency | Tasks | Notes |
|---|---|---|---|
| Pre-flight | sequential, manual | PF1–PF4 | Dispatch agent runs these and selects from §Pre-flight outcome matrix. |
| 1 | sequential | T0a → T0b | T0b's `if/elif` dispatch references the FDB case source seam from T0a. Same PR; commits in order. |
| 2A | **1×** | T1, T2, T6a | CANDOR path; tight coupling between loader, driver extension, tests. One coder agent. |
| 2B | **1×** | T3, T4, T6b | FDB V1 + V1.5; one coder agent because the two versions share a parent module and the inline mappers are tiny. |
| 3 | sequential | T5 → T6c | CLI surface, then CLI tests. |

**Max realistic concurrency: 2 agents** (Wave 2A and 2B run in parallel). Total task count is small enough that a single coder could ship the whole PR, but splitting Wave 2A/2B halves wall-clock time and keeps loader scope tight per agent.

---

## §Foundation-first ordering

**Pre-flight + Wave 1 are blocking gates.** No loader code (Wave 2A or 2B) is written until:
- PF1–PF4 outcomes are recorded in the PR description.
- T0a (FDB seam) is merged into the working branch — otherwise T3's `iter_cases(split)` cannot branch on `real`.
- T0b (runner dispatch) is merged — otherwise `python -m companion_harness.evals run --adapter candor` exits 2 ("not implemented in Phase A").

---

## §Task list

> Task numbering is local to v0.2c (T0a, T0b, T1…T6c). The v0.2 roadmap labels these collectively Tasks 12–14.

### T0a — Add `real`/`synthetic` flag + `NotImplementedError` seam to FDB v1/v1.5

**Files:** `companion_harness/evals/adapters/full_duplex_bench.py`.

**Change:**
- Add `synthetic: bool = True` to `FullDuplexBenchV1CaseSource` and `FullDuplexBenchV15CaseSource`.
- In each `iter_cases(split)`, branch: if `self.synthetic`, return existing cases; else `raise NotImplementedError("Real FDB ingestion requires HF datasets + accepted gate; see plan-v0.2c-execution.md T3.")`.
- Surface `synthetic` in the dataclass docstring.
- Existing `__post_init__` only builds synthetic cases when `synthetic=True` (move the synthetic-case build inside the branch to avoid wasted work in real mode).
- `build_v1()` and `build_v1_5()` factory signatures: add `synthetic: bool = True` passthrough.

**Success criterion:** `pytest tests/test_full_duplex_bench_adapter.py` still passes (synthetic default unchanged); a new `test_fdb_real_mode_raises_not_implemented` in T6b will turn green.

**Discipline note:** Mirror the CANDOR seam shape (`candor.py:101-108`) verbatim where possible.

---

### T0b — Extend `runners.py` to dispatch CANDOR + FDB v1 + FDB v1.5

**Files:** `companion_harness/evals/runners.py`.

**Change:**
- Replace the `if args.adapter != "harness_native"` rejection (`runners.py:108-114`) with an inline `if/elif/elif/elif/else` chain:
  ```
  if args.adapter == "harness_native":
      return _run_harness_native(args.output, args.split)
  elif args.adapter == "candor":
      return _run_candor(args.output, args.split, args.mode, args.limit)
  elif args.adapter == "full_duplex_bench_v1":
      return _run_fdb(args.output, args.split, args.mode, args.limit, version="v1")
  elif args.adapter == "full_duplex_bench_v1_5":
      return _run_fdb(args.output, args.split, args.mode, args.limit, version="v1.5")
  else:
      print(f"adapter '{args.adapter}' not implemented...", file=sys.stderr)
      return 2
  ```
- Add `_run_candor` and `_run_fdb` runner functions modeled on `_run_harness_native` (existing pattern at `runners.py:27-64`): build adapter with `synthetic = (mode == "synthetic")`, iterate `case_source.iter_cases(split)` (sliced to `[:limit]` if `limit is not None`), call `scenario_driver.run(case, ...)`, write `run.json`.
- No `_AdapterRegistry` class. No dispatch table. Three `elif` branches is too few to justify either (CLAUDE.md coding rule 2).

**Success criterion:** `python -m companion_harness.evals run --adapter candor` succeeds (synthetic by default); `--adapter full_duplex_bench_v1` succeeds; `--adapter full_duplex_bench_v1_5` succeeds; `--adapter bogus` still exits 2. All covered by T6c tests.

---

### T1 — `_load_candor_streaming(split)` + `_candor_row_to_evaluation_case(row)` for CANDOR

**Files:** `companion_harness/evals/adapters/candor.py`.

**Change:**
- Replace the `NotImplementedError` (line 105) with a call to `self._real_cases(split)` when `self.synthetic is False`.
- Add module-level `_CANDOR_HF_SLUG = "<slug>"` and `_CANDOR_REVISION = "<commit_hash>"` constants (slug filled at impl time per PF2).
- Add `_load_candor_streaming(split: str) -> Iterable[dict]`:
  ```
  from datasets import load_dataset
  ds = load_dataset(_CANDOR_HF_SLUG, split=split, streaming=True, revision=_CANDOR_REVISION)
  # Sort-by-stable-key fallback (OQ resolution): if streaming ordering is not
  # guaranteed at this revision, buffer-and-sort by (speaker_id, start_time).
  yield from _ordered(ds, key=_candor_stable_key)
  ```
- Add `_candor_row_to_evaluation_case(row: dict, index: int) -> EvaluationCase` mapper. Required fields:
  - `case_id`: `f"candor_real_{index:04d}"`
  - `inputs`: `{'audio_pcm_bytes': row['audio']['bytes'], 'sample_rate': row['audio']['sampling_rate'], 'turn_gap_ms': row.get('turn_gap_ms', 0.0), 'overlap_ms': row.get('overlap_ms', 0.0), 'backchannel_pause_ms': row.get('backchannel_pause_ms', 0.0), 'response_delay_ms': row.get('response_delay_ms', 0.0)}` — exact `row[...]` keys verified at impl time against the HF schema; field absence triggers skip (see skip-counter below).
  - `consent_class`: `"safe_eval_fixture"` (OQ resolution).
  - `benchmark_name`: `"candor"`, `benchmark_version`: `"v1"`.
  - All other fields mirror the synthetic mapper at `candor.py:115-135`.
- Add **row-skip running counter**: `_real_cases` increments `self._attempted` per row, `self._skipped` on row-mapper failure (try/except around the mapper; log `dataset_row_skip_failure` per skip). Counter is exposed via `self.skip_stats() -> dict` so the runner can include `{'attempted': N, 'skipped': M, 'skip_rate': M/N}` in `run.json`. This satisfies the `dataset_row_skip_rate < 0.05` gate measurement column.
- Loader init refuses if `os.environ.get("HF_TOKEN") is None` AND the HF cache does not already hold the gate-accepted token. Raise `RuntimeError("HF_TOKEN missing; set HF_TOKEN or run `huggingface-cli login` and accept the CANDOR gate at <URL>.")`.

**Success criterion:** `CandorCaseSource(synthetic=False).iter_cases("test")` yields at least one `EvaluationCase` against the live HF dataset; `pytest tests/test_candor_real_loader.py` (T6a) passes; `dataset_row_skip_rate < 0.05` on the smoke fixture.

**Discipline note:** No "configurability" beyond `synthetic` and `seed` already on the dataclass. No new dataclass fields. Slug + revision are module constants, not init params.

---

### T2 — Extend `CandorScenarioDriver` to process real audio rows

**Files:** `companion_harness/evals/adapters/candor.py`.

**Change:**
- In `CandorScenarioDriver.run()` (line 154), keep the existing 4-observation aggregation flow.
- If `inputs.get("audio_pcm_bytes")` is present, derive the four timing observations from the audio + provided timing metadata if non-zero, else fall back to the existing `inputs.get("turn_gap_ms", 0.0)` etc. **No audio decode in v0.2c.** Real-mode rows are expected to carry pre-computed timing fields alongside `audio_pcm_bytes`; the audio bytes are passed through `results` for Phase C examiner consumption (`results['audio_pcm_bytes_ref'] = case.case_id` — note: do NOT inline the bytes into `results`; record a reference to the case so the audio can be re-fetched from the CaseSource cache).
- If both pre-computed timings AND audio_pcm_bytes are absent → skip this case in the loader (T1's mapper raises `ValueError`, increments `_skipped`).

**Alternative if T2 reveals deeper redesign need:** Document the deferral in the PR description. The driver continues to use the timing fields only; audio_pcm_bytes is carried in `inputs` for downstream phases but unused in v0.2c metric computation. This is acceptable per the §Pre-flight outcome matrix.

**Success criterion:** `pytest tests/test_candor_real_loader.py::test_real_audio_row_runs_through_driver` passes.

---

### T3 — `_load_fdb_streaming(version, split)` for FDB V1 and V1.5

**Files:** `companion_harness/evals/adapters/full_duplex_bench.py`.

**Change:**
- Add `_FDB_V1_HF_SLUG`, `_FDB_V1_REVISION`, `_FDB_V15_HF_SLUG`, `_FDB_V15_REVISION` module constants (slugs at impl time per PF2).
- Inline `_load_fdb_v1_streaming(split)` inside `FullDuplexBenchV1CaseSource._real_cases(split)`. Same `load_dataset(...streaming=True, revision=...)` + stable-key sort pattern as CANDOR.
- Inline `_load_fdb_v15_streaming(split)` inside `FullDuplexBenchV15CaseSource._real_cases(split)`. **Do NOT factor into a shared helper** — three call sites (or even two) is below the threshold for shared abstraction per CLAUDE.md rule 2. Two near-identical 8-line generators are easier to read and easier to delete independently if one dataset gates differently.
- Per-source skip counter, same shape as CANDOR's.
- Same `HF_TOKEN`-missing fail-fast init check.

**Success criterion:** `FullDuplexBenchV1CaseSource(synthetic=False).iter_cases("test")` and `FullDuplexBenchV15CaseSource(synthetic=False).iter_cases("test")` each yield at least one `EvaluationCase` against the live HF datasets.

---

### T4 — `_fdb_v1_row_to_evaluation_case` + `_fdb_v15_row_to_evaluation_case`

**Files:** `companion_harness/evals/adapters/full_duplex_bench.py`.

**Change:**
- Two module-level mapper functions, inline below the loader functions. Each maps a streaming row to an `EvaluationCase`:
  - `case_id`: `f"fdb_v1_real_{index:04d}_{scenario}"` / `f"fdb_v1_5_real_{index:04d}_{scenario}"`.
  - `scenario`: pulled from the row's scenario / category field (key verified at impl time; if absent, use `"fdb_unspecified"` and increment skip counter to flag).
  - `inputs`: `{'audio_pcm_bytes': row['audio']['bytes'], 'sample_rate': row['audio']['sampling_rate'], 'real': True}`.
  - All other fields mirror `_make_synthetic_cases` (`full_duplex_bench.py:54-80`).
- V1 mapper handles the 10 scenarios from `_SCENARIOS_V1`. V1.5 mapper handles the 15 from `_SCENARIOS_V1_5`. Unrecognized scenario labels in the row → skip + increment counter (do NOT silently map to a default).

**Success criterion:** `pytest tests/test_fdb_real_loader.py::test_v1_mapper_handles_known_scenarios` and `::test_v15_mapper_handles_known_scenarios` pass.

---

### T5 — CLI `--mode {synthetic,real}` + `--limit N`

**Files:** `companion_harness/evals/runners.py`.

**Change:**
- Add `--mode` argument to the `run` subparser:
  ```
  run_p.add_argument("--mode", default="synthetic", choices=["synthetic", "real"], help="Adapter mode. synthetic (default) uses fixtures; real streams from HuggingFace.")
  ```
- Add `--limit` argument:
  ```
  run_p.add_argument("--limit", type=int, default=None, metavar="N", help="Cap the number of cases processed (per adapter). Omit for full corpus.")
  ```
- `_run_candor` and `_run_fdb` consume `mode` and `limit`. `_run_harness_native` ignores them (or accepts and ignores with a debug log; harness_native has no real-mode concept).
- **No `--filter K=V`.** OQ-resolved above; do not add a `K=V` parser. If someone later needs row filtering, they file a follow-on.
- Runner emits `mode`, `adapter`, `limit`, `skip_stats` into `run.json` header so smoke vs full-run is unambiguous.

**Success criterion:** `python -m companion_harness.evals run --adapter candor --mode real --limit 5` runs end-to-end against the live dataset and writes `run.json` with `skip_stats.skip_rate < 0.05`.

---

### T6a — Tests: CANDOR real-mode loader

**Files:** `tests/test_candor_real_loader.py` (new).

**Cases:**
- `test_synthetic_default_unchanged` — `CandorCaseSource()` still yields 100 cases (regression guard on T1's branching change).
- `test_real_mode_requires_hf_token` — with `HF_TOKEN` unset, `CandorCaseSource(synthetic=False).iter_cases("test")` raises `RuntimeError` with the actionable message.
- `test_real_mode_smoke_yields_cases` — **marked `@pytest.mark.gpu`** (or `@pytest.mark.real_corpus`; new marker added in `pyproject.toml` if not present) — runs only on b200 dev or wherever `HF_TOKEN` is set; consumes the first 5 streamed rows; asserts each is a valid `EvaluationCase` with `inputs['audio_pcm_bytes']` non-empty.
- `test_skip_counter_tracks_malformed_rows` — feeds a fake row generator (monkeypatch `load_dataset`) with 10 good rows + 1 malformed; asserts `skip_stats == {'attempted': 11, 'skipped': 1, 'skip_rate': 1/11}`.
- `test_real_audio_row_runs_through_driver` — feeds one real-mode `EvaluationCase` (mock-constructed, no HF call) through `CandorScenarioDriver.run()`; asserts `ReplayRun.results` contains the four timing observation lists.

**Discipline note:** Tests that require live HF access carry the `real_corpus` marker and are excluded from default `pytest tests/` per `pyproject.toml [tool.pytest.ini_options]` marker config (mirror the existing `gpu` marker pattern).

---

### T6b — Tests: FDB real-mode loader

**Files:** `tests/test_fdb_real_loader.py` (new).

**Cases:**
- `test_v1_synthetic_default_unchanged` — `FullDuplexBenchV1CaseSource()` still yields 50 cases.
- `test_v15_synthetic_default_unchanged` — `FullDuplexBenchV15CaseSource()` still yields 50 cases.
- `test_fdb_v1_real_mode_raises_when_seam_only` — if PF2 fails for FDB (no slug locatable), the `_real_cases` method raises `NotImplementedError` with a follow-on issue reference. This test guards the partial-Wave outcome path.
- `test_fdb_v1_real_mode_smoke_yields_cases` — `@pytest.mark.real_corpus`; first 5 rows; valid `EvaluationCase`s.
- `test_fdb_v15_real_mode_smoke_yields_cases` — same, V1.5.
- `test_v1_mapper_handles_known_scenarios` — pass synthetic rows for each `_SCENARIOS_V1` label; assert correct `scenario` field on output.
- `test_v15_mapper_handles_known_scenarios` — same, V1.5.

---

### T6c — Tests: CLI surface

**Files:** `tests/test_eval_runner_cli.py` (extend existing file if present; otherwise new).

**Cases:**
- `test_candor_adapter_dispatches` — `main(["run", "--adapter", "candor"])` returns 0 (synthetic default, no HF call).
- `test_fdb_v1_adapter_dispatches` — same for `full_duplex_bench_v1`.
- `test_fdb_v1_5_adapter_dispatches` — same for `full_duplex_bench_v1_5`.
- `test_bogus_adapter_still_exits_2` — `main(["run", "--adapter", "bogus"])` returns 2.
- `test_mode_real_requires_hf_token` — `main(["run", "--adapter", "candor", "--mode", "real"])` without `HF_TOKEN` returns nonzero and prints the actionable error.
- `test_limit_caps_cases` — synthetic-mode, `--limit 3` → `run.json` has exactly 3 case entries.
- `test_run_json_carries_mode_adapter_limit_skip_stats` — `run.json` header includes all four fields.

---

## §Numeric gates with Measurement column

| Metric | Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|---|
| `dataset_row_skip_rate` | < 0.05 | T1/T3 coder | `skipped / attempted` from the live running counter in `CandorCaseSource._skip_stats` / `FullDuplexBench*CaseSource._skip_stats`; emitted into `run.json` under `skip_stats` per adapter | Per `--mode real` run | blocking |
| `real_mode_eval_score_replay_match_rate` | == 1.0 | T1/T3 coder | Two real-mode runs of the same fixture at the same pinned `revision=`; exact `BenchmarkResult.score` match across runs (verified by T6a's `test_real_mode_smoke_yields_cases` deterministic re-run, marked `real_corpus`) | All real-mode smoke fixtures (5 cases each, 3 datasets = 15 cases) | blocking |
| `real_mode_smoke_wall_clock_p95_ms` | < 60_000 (60 s for `--limit 200`) | T5 coder | Wall-clock for `python -m companion_harness.evals run --adapter <name> --mode real --limit 200`, measured at runner end-of-run; recorded in `run.json` under `timing_ms` | Per `--mode real --limit 200` run | blocking |
| Full-corpus wall-clock | **no gate** | n/a | Full real-mode runs (no `--limit`) are operator-scheduled; v0.2c does not gate them | n/a | n/a |
| `local_ci_pass_rate` | == 1.0 | T6a/T6b/T6c coder | `pytest tests/` (default markers; excludes `real_corpus` and `gpu`) | All v0.2c contract tests | blocking |
| `remote_smoke_pass_rate` | == 1.0 | T6a/T6b/T6c coder | `pytest -m real_corpus tests/` on b200 with `HF_TOKEN` set | All `real_corpus`-marked tests | blocking |

**Scoping note on wall-clock:** The 60 s gate is scoped to `--limit 200` only. Per the OQ resolution, full-corpus runs (potentially hours for CANDOR's ~50 GB) carry no gate because they are operator-scheduled and tagged in the replay-report header.

---

## §Out of scope (deferred, not forgotten)

- **`--filter K=V`** — deferred per OQ resolution; file follow-on if a use case appears.
- **New `consent_class` vocabulary** for licensed corpora — reuse `"safe_eval_fixture"` for v0.2c.
- **Audio decode / re-VAD inside `CandorScenarioDriver`** — v0.2c carries `audio_pcm_bytes` for downstream phases but does not decode in this milestone.
- **Caching strategy beyond HF datasets default** — `~/.cache/huggingface/` is the cache; no custom budget enforcement in v0.2c (the roadmap §Dataset governance note about "local cache budget" is documented in `eval-quickstart.md` per the roadmap, not enforced in code here).
- **Live-examiner Phase C consumption of `audio_pcm_bytes`** — Wave 4 of v0.2 (`LiveExaminerCaseSource`, gated on Wave 2 diarization).
- **CANDOR or FDB row schema evolution** — pinned `revision=` insulates v0.2c; revision bumps are deliberate PRs.

---

## §Verifiable success criteria (per CLAUDE.md coding rule 4)

This PR ships exactly one outcome: **(b) implementations that turn the following `pytest.skip`s and "not implemented" branches into passing tests + working real-mode loaders.**

Aggregate success: `pytest tests/test_candor_real_loader.py tests/test_fdb_real_loader.py tests/test_eval_runner_cli.py` passes with default markers, AND on b200 with `HF_TOKEN` set, `pytest -m real_corpus tests/` passes, AND `python -m companion_harness.evals run --adapter candor --mode real --limit 5` produces a valid `run.json` with `skip_stats.skip_rate < 0.05`.

---

## §Cross-references

- Roadmap: `docs/roadmap-v0.2-draft.md` §Wave 3 (Tasks 12–14), §Dataset governance, §Numeric gates table rows `dataset_row_skip_rate` + `real_mode_eval_score_replay_match_rate`.
- Spec: `docs/architecture-v0.1.md` §Part 5 (`EvaluationCase` schema — DO NOT modify in v0.2c).
- Adjacent code: `companion_harness/evals/adapters/candor.py` (CANDOR seam shape to mirror), `companion_harness/evals/adapters/full_duplex_bench.py` (FDB seam to add), `companion_harness/evals/runners.py` (dispatch chain to extend), `companion_harness/schemas.py:277-302` (`EvaluationCase` shape — `inputs: dict | None` is the audio carrier).
- Discipline: `CLAUDE.md` §Coding discipline rules 1–4 (especially rule 2: no abstractions for single-use; rule 3: surgical edits, no opportunistic refactor).
- Dependency: `pyproject.toml [project.optional-dependencies] eval` (already lists `datasets>=2.16`, `soundfile>=0.12`; no new deps in v0.2c).

---

## §Status of this plan

- Pre-flight outcomes: **not yet executed**. The dispatch agent runs PF1–PF4 first and selects from the §Pre-flight outcome matrix.
- Wave 2 scope is **conditional** on PF2 (HF slug locatability) and PF3 (gate acceptance). The PR description must state which Wave 2 branch was taken.
- Plan-critic round: **not yet converged**. This is the first-draft execution plan; expect plan-critic feedback before sub-agent dispatch.
