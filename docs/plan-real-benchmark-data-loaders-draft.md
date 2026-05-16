# Plan — Real benchmark data loaders (CANDOR + FullDuplexBench) — DRAFT

## Status

**DRAFT — 2026-05-15.** Replaces `NotImplementedError` real-mode raises in `CandorCaseSource` (PR #176). Wave 2 (FullDuplexBench) is conditional — see Pre-flight verification section below.

## Why

Today `CandorCaseSource` defaults to synthetic mode (small fixture cases hand-coded into the adapter). Real mode raises `NotImplementedError("CANDOR dataset not yet bundled; install datasets and add a real-mode loader.")`. Whether `FullDuplexBenchV1/V1.5CaseSource` have an equivalent real-mode seam is unverified — see Pre-flight verification in Wave 2.

Real benchmark scores are the secondary regression probe per the architecture spec — they tell us when we drift behaviorally from the field's baseline. Without them, we're only validating against our own synthetic corpus.

## Goal

Add HuggingFace `datasets` loaders behind the existing `real=True` flag on each case source. Same Protocol surface, same `EvaluationCase` schema; only the case stream changes.

## Anchor decisions — non-transient

### Anchor 1 — `datasets` library, no custom downloader

Use HuggingFace `datasets` library (already an optional `[eval]` dependency in `pyproject.toml`). Justification:
- Both CANDOR and FullDuplexBench are published on HuggingFace.
- `datasets` handles caching, lazy loading, sharding, format conversion.
- No custom HTTP/tar/zip logic to maintain.

**Rejected:** raw HuggingFace Hub API calls + manual file management. Premature optimization.

### Anchor 2 — Streaming-mode default (no full download)

Use `streaming=True` in `load_dataset()`. Justification:
- CANDOR is multi-GB; full download is operator-hostile.
- Streaming mode lets the case-source iterator pull rows lazily.
- First-time-only download is via the same code path; cached locally.

`HF_HUB_CACHE` env var (already set on b200) governs cache location.

### Anchor 3 — Schema mapping is per-adapter

Each adapter implements its own `_hf_row_to_evaluation_case(hf_row) -> EvaluationCase` mapper. Justification:
- CANDOR rows have different fields than FullDuplexBench rows.
- Per-adapter mappers keep schema-translation logic close to the dataset that needs it.
- Common `EvaluationCase` fields populated: `case_id`, `stage`, `scenario`, `modalities`, `consent_class`, `benchmark_name`, `benchmark_version`. Raw audio is stored in `EvaluationCase.inputs` as `{'audio_pcm_bytes': bytes, 'sample_rate': int}` — `inputs` is a freeform dict per the existing schema. Callers must not assume an `audio_bytes` top-level field.

**Rejected:** generic adapter-agnostic mapper. Adds coupling that pays back nothing.

### Anchor 4 — Real-mode requires explicit opt-in per run

`--mode {synthetic,real}` flag on the eval CLI; default `synthetic`. Justification:
- Real-mode runs may take hours; surprising for someone testing the eval pipeline itself.
- Operator opts in deliberately ("I want benchmark scores tonight").

## Open questions (leans)

- **OQ-1**: Dataset version pinning. CANDOR has revisions; we want bit-identical scores across runs. **Lean**: pin `revision=` in `load_dataset()` to a specific commit hash AND verify against the actual CANDOR HF repo that case ordering is stable across that pinned commit. If LFS-backed audio + separately-versioned metadata parquet break case-id stability, the loader emits a deterministic case ordering by sorting rows on a stable key (e.g., `conversation_id`) before iteration. Bump only deliberately.
- **OQ-2**: FullDuplexBench V1 vs V1.5 — separate adapters or one with a version flag? **Lean**: separate adapters (already so per current code). V1 and V1.5 have different case taxonomies.
- **OQ-3**: Subset selection (e.g., only conversations < 60s for fast iteration)? **Lean**: `--limit N` CLI flag only (post-stream). If multi-key filtering becomes a real requirement, add it then. Single CLI flag is the minimum.
- **OQ-4**: Privacy / consent class for real benchmark audio? **Lean**: use existing `consent_class="safe_eval_fixture"` with a note in the loader: 'Public dataset under license; legal review pending per retention policy.' Adding a new `consent_class` value requires updating the retention-policy table — defer.
- **OQ-5**: Handling rows that don't fit our `EvaluationCase` shape (missing audio, wrong format)? **Lean**: log a `dataset_row_skipped` event with reason; continue iterator; report skip count in final report.

## Tasks

### Wave 0 — Pre-Wave-1 prerequisites

- **T0b**: Extend `companion_harness/evals/runners.py` to dispatch via an adapter registry. Use the existing `BenchmarkAdapter` Protocol; allow `candor`, `full_duplex_bench_v1`, `full_duplex_bench_v1_5` as named adapters. The `--mode real` flag is meaningless without this dispatch in place. Reference plan #256 (eval-console PR1) for the same registry design. T1–T3 (and any Wave 2 tasks) cannot ship until this dispatch path is wired.

### Wave 1 — CANDOR

**Prerequisite:** CANDOR HuggingFace gated-access agreement signed AND `HF_TOKEN` available in env on b200. Verify with `huggingface-cli whoami` before starting T1.

- **T1**: `_load_candor_streaming(split: str) -> Iterator[dict]` helper in `companion_harness/evals/adapters/candor.py`. Uses `datasets.load_dataset("candor/candor", split=split, streaming=True, revision=...)`.
- **T2**: `_candor_row_to_evaluation_case(row) -> EvaluationCase` mapper.
- **T3**: Replace the `NotImplementedError` in `CandorCaseSource.iter_cases()` real-mode branch with a call to the new helper + mapper.

### Wave 2 — FullDuplexBench V1 + V1.5 (CONDITIONAL — see pre-flight below)

**Pre-flight verification (must pass before Wave 2 begins):**

1. Inspect `companion_harness/evals/adapters/full_duplex_bench.py` and confirm whether `FullDuplexBenchV1CaseSource` and `FullDuplexBenchV15CaseSource` have a `real`/`synthetic` flag and `NotImplementedError` raise paths. The plan-critic observed these may be absent on the current branch.
2. If the seam is missing: Wave 2 must first add the `real`/`synthetic` flag + `NotImplementedError` raise as a separate pre-Wave-2 task (**T0a**: "Add real-mode opt-in seam to FullDuplexBenchV1/V1.5 case sources"). T4–T6 are blocked on T0a.
3. Locate the actual HuggingFace slug for FullDuplexBench V1 and V1.5. The slug `fullduplexbench/fullduplexbench` used in this plan is **unverified**. If the dataset is not published on HuggingFace with stable revisions, defer Wave 2 entirely to a follow-on PR once a dataset source is identified. Document the confirmed slug + revision in the loader before any code is written.

**Wave 2 status: deferred until pre-flight items above are resolved.** If the HF slug does not exist, Wave 2 is out of scope for this PR.

- **T0a** (if seam missing): Add `real=False` flag + `NotImplementedError` raise to `FullDuplexBenchV1CaseSource` and `FullDuplexBenchV15CaseSource`.
- **T4** (blocked on T0a + slug verification): Inline `load_dataset()` directly in each case source. Do not share a version-routing helper — implement `_load_fdb_v1_streaming()` in the V1 adapter and `_load_fdb_v15_streaming()` in the V1.5 adapter separately.
- **T5**: Per-version row mappers `_fdb_v1_row_to_evaluation_case`, `_fdb_v15_row_to_evaluation_case`.
- **T6**: Replace `NotImplementedError` in both V1 and V1.5 case sources.

### Wave 3 — CLI integration
- **T7**: `--mode {synthetic,real}` flag and `--limit N` flag on `python -m companion_harness.evals run`. Default synthetic. `--limit N` applies post-stream.

### Wave 4 — Contract tests
- **T9a** (ships with Wave 1 PR):
  - `test_candor_real_mode_iter_cases_returns_evaluation_cases` (mocked HF response)
  - `test_candor_dataset_version_pinned` (assert revision is set)
  - `test_real_mode_requires_eval_extra_installed` (graceful error when `datasets` is missing)
- **T9b** (ships with Wave 2 PR, if Wave 2 proceeds):
  - `test_fdb_v1_real_mode_iter_cases`
  - `test_fdb_v15_real_mode_iter_cases`
- **T9c** (ships with T7):
  - `test_limit_applies_post_stream`

## Numeric gates

| Metric | Gate (advisory, real-mode only) |
|---|---|
| `candor_real_mode_case_count_per_run` | > 100 (otherwise filter too restrictive) |
| `fdb_v1_real_mode_case_count_per_run` | > 50 |
| `fdb_v15_real_mode_case_count_per_run` | > 50 |
| `dataset_row_skip_rate` | < 0.05 — `skipped / attempted` where both are counted live by the iterator; reported in the final `run.json` artifact. Streaming mode does not provide a pre-stream total; ratio is computed post-hoc from the running counter. |
| `real_mode_run_wall_clock_minutes` | < 60 ONLY with `--limit 200` (default smoke). Full-corpus runs are operator-scheduled and have no wall-clock gate. |

## Risks

1. **HuggingFace gate / auth (hard prerequisite)**: CANDOR is gated access. The CANDOR HuggingFace gated-access agreement must be signed AND `HF_TOKEN` must be available in env on b200 before T1 begins. Verify with `huggingface-cli whoami` before Wave 1. T1 cannot begin until this is confirmed. Document in `eval-quickstart.md`.
2. **Dataset schema drift**: HF datasets can revise schemas. Mitigation: pin `revision=`; CI test that mapper still works against pinned revision.
3. **Long real-mode runs**: hours, not minutes. Mitigation: `--limit` flag for quick smoke; full runs are operator-scheduled.
4. **Disk usage in cache**: multi-GB. Mitigation: rely on streaming-mode default; document `HF_HUB_CACHE` location.
5. **License compliance**: ensure we respect dataset licenses (some are research-only, no commercial use). Mitigation: emit `consent_class` per dataset; legal review before any public benchmark publication.

## Out of scope

- New dataset adapters (VoiceBench / VocalBench / HumDial-FDBench real-mode is its own follow-up plan).
- Real-mode synthetic-mode parity tests (numeric scores will differ; that's the whole point).
- Continuous benchmarking (nightly cron, dashboards) — operator-driven for now.

## Cross-references

- `docs/eval-subsystem-spec.md` — eval subsystem spec.
- `docs/eval-quickstart.md` — operator CLI quickstart.
- `companion_harness/evals/adapters/candor.py` — current synthetic-mode CandorCaseSource.
- `companion_harness/evals/adapters/full_duplex_bench.py` — current synthetic-mode V1/V1.5 case sources.
- CANDOR: https://huggingface.co/datasets/candor/candor
- FullDuplexBench: https://huggingface.co/datasets/fullduplexbench/fullduplexbench (slug UNVERIFIED — must confirm before Wave 2)
