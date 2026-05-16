# Plan — Real benchmark data loaders (CANDOR + FullDuplexBench) — DRAFT

## Status

**DRAFT — 2026-05-15.** Replaces `NotImplementedError` real-mode raises in `CandorCaseSource` (PR #176) and `FullDuplexBenchV1/V1.5CaseSource` (PR #178). Lets real benchmark scores land.

## Why

Today both case sources default to synthetic mode (small fixture cases hand-coded into the adapter). Real mode raises `NotImplementedError("CANDOR dataset not yet bundled; install datasets and add a real-mode loader.")`.

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
- Common `EvaluationCase` fields (case_id, audio_bytes, expected_metric, consent_class) are computed in the mapper.

**Rejected:** generic adapter-agnostic mapper. Adds coupling that pays back nothing.

### Anchor 4 — Real-mode requires explicit opt-in per run

`--mode {synthetic,real}` flag on the eval CLI; default `synthetic`. Justification:
- Real-mode runs may take hours; surprising for someone testing the eval pipeline itself.
- Operator opts in deliberately ("I want benchmark scores tonight").

## Open questions (leans)

- **OQ-1**: Dataset version pinning. CANDOR has revisions; we want bit-identical scores across runs. **Lean**: pin `revision=` in `load_dataset()` to a specific commit hash; bump only deliberately.
- **OQ-2**: FullDuplexBench V1 vs V1.5 — separate adapters or one with a version flag? **Lean**: separate adapters (already so per current code). V1 and V1.5 have different case taxonomies.
- **OQ-3**: Subset selection (e.g., only conversations < 60s for fast iteration)? **Lean**: `--limit N` CLI flag + `--filter <key=value>` for category filtering. Both apply post-stream.
- **OQ-4**: Privacy / consent class for real benchmark audio? **Lean**: `consent_class="public_dataset_under_license"`; retention per dataset's license terms.
- **OQ-5**: Handling rows that don't fit our `EvaluationCase` shape (missing audio, wrong format)? **Lean**: log a `dataset_row_skipped` event with reason; continue iterator; report skip count in final report.

## Tasks (sketch, 9 tasks)

### Wave 1 — CANDOR
- **T1**: `_load_candor_streaming(split: str) -> Iterator[dict]` helper in `companion_harness/evals/adapters/candor.py`. Uses `datasets.load_dataset("candor/candor", split=split, streaming=True, revision=...)`.
- **T2**: `_candor_row_to_evaluation_case(row) -> EvaluationCase` mapper.
- **T3**: Replace the `NotImplementedError` in `CandorCaseSource.iter_cases()` real-mode branch with a call to the new helper + mapper.

### Wave 2 — FullDuplexBench V1 + V1.5
- **T4**: `_load_fdb_streaming(version, split)` helper in `full_duplex_bench.py`. Handle V1 vs V1.5 routing.
- **T5**: Per-version row mappers `_fdb_v1_row_to_evaluation_case`, `_fdb_v15_row_to_evaluation_case`.
- **T6**: Replace `NotImplementedError` in both V1 and V1.5 case sources.

### Wave 3 — CLI integration
- **T7**: `--mode {synthetic,real}` flag on `python -m companion_harness.evals run`. Default synthetic.
- **T8**: `--limit N` and `--filter K=V` flags (post-stream filtering).

### Wave 4 — Contract tests
- **T9**:
  - `test_candor_real_mode_iter_cases_returns_evaluation_cases` (mocked HF response)
  - `test_candor_dataset_version_pinned` (assert revision is set)
  - `test_fdb_v1_real_mode_iter_cases`
  - `test_fdb_v15_real_mode_iter_cases`
  - `test_real_mode_requires_eval_extra_installed` (graceful error when `datasets` is missing)
  - `test_limit_filter_apply_post_stream`

## Numeric gates

| Metric | Gate (advisory, real-mode only) |
|---|---|
| `candor_real_mode_case_count_per_run` | > 100 (otherwise filter too restrictive) |
| `fdb_v1_real_mode_case_count_per_run` | > 50 |
| `fdb_v15_real_mode_case_count_per_run` | > 50 |
| `dataset_row_skip_rate` | < 0.05 (5% of rows skipped is suspicious) |
| `real_mode_run_wall_clock_minutes` | < 60 (with default settings) |

## Risks

1. **HuggingFace gate / auth**: some datasets require user agreement. Mitigation: document in `eval-quickstart.md`; provide instructions for `huggingface-cli login`.
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
- FullDuplexBench: https://huggingface.co/datasets/fullduplexbench/fullduplexbench
