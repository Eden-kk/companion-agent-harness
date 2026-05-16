# Plan — Eval console PR1 (run launcher + artifact browser) — DRAFT

## Status

**DRAFT — 2026-05-15.** Converged from a two-round critique between Claude and GPT-5.4 (see meta notes at bottom). Ready for `/plan-review` to produce the final converged plan; then fix-coder.

## Goal

Ship a single PR that lets the operator launch eval runs and browse artifacts from a `/eval` page on the existing manual-test console (port 8800). **No failure inspector**, **no Tier-B replay controls**, **no compare-runs** — those land in subsequent PRs once real failure slicing exists.

## Anchor decisions — non-transient (locked)

### Anchor 1 — Same process, same port: `/eval` route on existing `manual_test_console.server`

Add `/eval` + `/eval/*` routes to `manual_test_console/server.py`. Same aiohttp app, same port 8800, same `/healthz`. The current console header gains a `[Live] [Eval]` switcher.

**Rejected:** new server on port 8810. Operator friction for single-user tool.
**Rejected:** web/UI under `companion_harness/evals/web/`. Violates the eval package's import-direction discipline (eval logic stays pure; web/UI is a delivery concern).

### Anchor 2 — Adapter registry as the source of truth

New module `companion_harness/evals/registry.py` exposing:

```python
@dataclass(frozen=True)
class AdapterInfo:
    name: str                      # "harness_native" | "vocalbench" | ...
    version: str                   # "v1" | "synthetic-v1"
    status: Literal["ready", "synthetic_only", "disabled"]
    case_count: int | None         # known at synthetic-mode init; None if unknown
    supports_real_mode: bool
    supports_synthetic_mode: bool
    build: Callable[[], CaseSource]
    notes: str | None              # e.g. "real ingestion requires datasets extra"

ADAPTERS: dict[str, AdapterInfo] = {
    "harness_native": AdapterInfo(...),
    "vocalbench":     AdapterInfo(..., status="synthetic_only", notes="real ingestion deferred"),
    "voicebench":     AdapterInfo(..., status="synthetic_only", notes="real ingestion deferred"),
    "humdial_fdbench":AdapterInfo(..., status="synthetic_only", notes="real ingestion deferred"),
    "candor_stats":   AdapterInfo(..., status="synthetic_only", notes="real CANDOR data deferred"),
    "full_duplex_bench_v1": AdapterInfo(..., status="synthetic_only", notes="real FDB data deferred"),
}
```

**Justification (per GPT's catch):** today `companion_harness/evals/runners.py` only dispatches `harness_native`; other adapters exist as case-source classes but aren't CLI-runnable. The registry centralizes this knowledge so the web UI and CLI share the same dispatch table — no overclaiming, no "this exists but you can't run it" state.

### Anchor 3 — Registry-and-dispatch update lands in the same PR as the web routes

PR1 must include:
1. `companion_harness/evals/registry.py` (new).
2. Update `companion_harness/evals/runners.py` to dispatch via the registry (replaces the hardcoded `harness_native`-only switch).
3. Web routes (`manual_test_console/eval_routes.py`).
4. Frontend (`manual_test_console/eval.html` + `eval_app.js` + `eval_style.css`).

**Justification:** shipping the registry without web is a partial improvement; shipping web without registry overclaims. Bundling them is one PR with one outcome.

### Anchor 4 — No failure inspector, no `suggested_fix` in PR1

The failure-inspector lands in PR2, paired with `CausalFailureSliceExtractor` wired into at least one adapter. PR1 shows a placeholder section ("Failure inspector — coming in PR2; requires CausalFailureSliceExtractor wiring") rather than an empty panel.

`suggested_fix` UI rule: **render only if `failure_slice.suggested_fix is not None`**. No canned advice.

**Justification:** every Phase D adapter currently uses `NoOpFailureSlicer`. A failure-inspector UI shown today would display "Failure slices not generated" on every case — hollow UX. Better to ship run-browsing as the hero and add the inspector when there's real data.

### Anchor 5 — Visual: synthetic = first-class badge, raw schema fields in detail view

Every synthetic case displays a `SYNTHETIC` badge in the case row (not just in the adapter dropdown). Event-detail view shows the raw `Event` schema fields: `payload_kind`, `subject_class`, `sensitivity`, `retention_policy_id` (compact label OK in summary; raw fields visible on row expansion).

**Justification:** prevents the UI from masking the synthetic-vs-real distinction. Honesty signal matches the harness's culture.

## Open questions (leans)

- **OQ-1**: Should `/eval/runs` list runs from disk (`reports/<run_id>/`) or from an in-memory registry that resets on server restart? **Lean**: disk-backed — runs survive restarts and the operator can compare across sessions.
- **OQ-2**: Should `POST /eval/runs` block until the run finishes, or return immediately with a run_id and stream status via WebSocket? **Lean**: return immediately + WebSocket. Run wall time can be minutes.
- **OQ-3**: Where do reports/ output go on disk? **Lean**: `<blob_dir>/eval_reports/` per session, with `--eval-reports-dir` CLI flag to override. Keeps eval artifacts separate from manual-session blobs.
- **OQ-4**: How are running runs cancelled (browser refresh, explicit Stop button)? **Lean**: explicit `POST /eval/runs/{run_id}/cancel` only — no auto-cancel on disconnect. Operator may want to walk away from a long real-mode run.
- **OQ-5**: Should the eval registry be importable from the CLI runner without circular issues? **Lean**: yes — runner imports registry; registry imports adapter modules; adapter modules don't import registry. One-way.

## Tasks (sketch, 6 tasks — all in one PR)

### Wave 1 — Registry + runner dispatch
- **T1**: `companion_harness/evals/registry.py` — `AdapterInfo` + populated `ADAPTERS` table for 6 adapters.
- **T2**: `companion_harness/evals/runners.py` — replace hardcoded switch with registry-based dispatch. Error message when adapter unknown should reference `gh issue` to file new-adapter requests.

### Wave 2 — Web routes
- **T3**: `manual_test_console/eval_routes.py` — handlers for:
  - `GET /eval` → static `eval.html`.
  - `GET /eval/adapters` → JSON list from registry.
  - `GET /eval/runs` → list runs from `reports/`.
  - `POST /eval/runs` → launch run, return `{run_id, status: "started"}`.
  - `GET /eval/runs/{run_id}` → manifest + metrics.
  - `GET /eval/runs/{run_id}/event_logs/{case_id}` → JSONL stream.
- **T4**: `manual_test_console/server.py` — register the new routes in `build_app()`.

### Wave 3 — Frontend
- **T5**: `manual_test_console/eval.html` + `eval_app.js` + `eval_style.css`. Reuse current console's dark/monospace operator-style vocabulary; add `[Live] [Eval]` switcher in the header bar (also added to `index.html`).
- Components: HeaderBar, CaseList, RunSummary, EventLogTabs, ArtifactLinks. **No** FailureInspector.

### Wave 4 — Tests
- **T6**: 
  - `tests/test_eval_registry.py` — registry shape, all 6 adapters present, build callables work.
  - `tests/test_eval_routes.py` — each route returns expected shape; launch + poll + read event log; cancel route.
  - `tests/test_eval_console_html_renders.py` — static HTML serves, contains the expected component anchors.

## Numeric gates

| Metric | Gate |
|---|---|
| `eval_adapter_dispatch_coverage` | All 6 registry entries dispatchable from web |
| `eval_route_request_latency_ms_p95` | < 100ms (excluding the run itself) |
| `eval_html_renders_without_js_errors` | true |
| Targeted test count | ≥ 12 new |

## Risks

1. **Registry-runner coupling regression**: changing `runners.py` dispatch may break existing `harness_native`-only callers. Mitigation: keep the legacy code path for one cycle behind a feature flag; remove in PR2 when registry is proven.
2. **`reports/` disk growth**: each run produces metrics + event logs + report.md. Mitigation: defer retention policy to v0.2 Wave 6 (Task 18 — `--blob-retention-days`). For PR1, document the manual cleanup workflow.
3. **WebSocket complexity for live progress**: not strictly needed for PR1 — synchronous polling on `GET /eval/runs/{run_id}` is enough. Mitigation: WebSocket is OQ-2 lean; can defer if it adds complexity.
4. **Browser autoplay / static-file caching**: same risk as the live console; mitigate by using same aiohttp static-file serving pattern.

## Out of scope (deferred to subsequent PRs)

- **PR2**: Failure inspector + `CausalFailureSliceExtractor` wired into harness_native + at least one Phase D adapter.
- **PR3**: Report/artifact polish (report.md preview, download bundle).
- **PR4**: Tier-B replay controls + policy-decision diff view.
- **PR5+**: Compare runs (deferred indefinitely until stable baseline + regression rubric).

## Cross-references

- Eval spec: `docs/eval-subsystem-spec.md`.
- Eval quickstart: `docs/eval-quickstart.md`.
- Manual-test handbook: `docs/manual-test-handbook.md`.
- Live console implementation: `manual_test_console/server.py`, `manual_test_console/index.html`.
- Runtime → eval import-direction invariant: `tests/test_runtime_does_not_import_evals.py`.
- v0.2 milestone: `docs/roadmap-v0.2-draft.md` — this PR is NOT v0.2 scope; it's a parallel deliverable that ships eval-side capability for the operator's iteration loop.

## §Meta — how this plan converged

Two-round critique between Claude (this assistant) and GPT-5.4. Key convergence points:

| Topic | Original GPT plan | Claude critique | Final convergence |
|---|---|---|---|
| Web layer location | `companion_harness/evals/web/` | violates eval-package import discipline | **`manual_test_console/eval_routes.py`** (Anchor 1) |
| Port | Add new server on 8810 | operator friction | **Reuse 8800** (Anchor 1) |
| PR1 hero | Failure inspector | empty inspector is hollow | **Run launcher + artifact browser** (Anchor 4) |
| `suggested_fix` | Always rendered | hallucinated diagnosis | **Render only when populated** (Anchor 4) |
| Phase D adapters | Disabled in dropdown | overclaiming opposite — they exist | **List with `synthetic_only` capability tag** (Anchor 2) |
| Adapter dispatch | Implicit | Claude missed that runner only dispatches `harness_native` today | **Registry-and-dispatch in same PR** (Anchor 3) |
| `runnable_from_web` field | Yes | redundant — web is thin wrapper over CLI | **Dropped; use `status` + `case_count` only** |
| Compare runs | PR5 | premature | **Deferred indefinitely** (Out of scope) |

Round 1: Claude critique of GPT's original design (caught 7 issues).
Round 2: GPT's response (accepted 80–85%, added the registry catch + capability matrix + raw-schema-fields refinement).
This plan: converged ready-for-review version.
