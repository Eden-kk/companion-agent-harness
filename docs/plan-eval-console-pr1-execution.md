# Plan — Eval Console PR1 (run launcher + artifact browser) — EXECUTION

## Status

**EXECUTION PLAN — 2026-05-16.** Converged execution plan derived from the merged design `docs/plan-eval-console-pr1-draft.md` (5 anchors, 5 OQs, 6 design tasks). Structured as **6 parallel sub-PRs (E1–E6)** with an explicit file-conflict map so coder agents can fan out without stepping on each other.

This plan is for executors. The "why" lives in the draft; this document covers the "how" and "in what order".

## Goal

Ship the operator-facing `/eval` page on the existing manual-test console (port 8800) with run-launcher + artifact-browser capability, plus the AdapterInfo registry that the launcher and CLI both consume. **No failure inspector**, **no Tier-B replay controls**, **no compare-runs** (deferred to PR2+).

## Cross-link

- Design source: [`docs/plan-eval-console-pr1-draft.md`](plan-eval-console-pr1-draft.md) — anchors, OQs, rejected alternatives, meta convergence notes.
- Eval protocols: [`companion_harness/evals/protocols.py`](../companion_harness/evals/protocols.py).
- Eval quickstart: [`docs/eval-quickstart.md`](eval-quickstart.md).
- Manual-test handbook: [`docs/manual-test-handbook.md`](manual-test-handbook.md).
- Live console implementation: `manual_test_console/server.py`, `manual_test_console/index.html`.
- Runtime → eval import-direction invariant: `tests/test_runtime_does_not_import_evals.py`.

## Anchor decisions (inherited — LOCKED)

These were converged in the draft and MUST hold across all sub-PRs. Repeated here so each sub-PR coder sees them without re-reading the draft.

1. **Same process, same port (8800).** `/eval` route lives on existing `manual_test_console.server`. No new server on 8810.
2. **AdapterInfo registry is the source of truth.** Web UI and CLI runner both dispatch through `companion_harness/evals/registry.py`.
3. **Registry-and-dispatch update lands in the same PR as the web routes** (per draft Anchor 3). This execution plan splits that bundle into 6 sub-PRs (E1–E6) that merge together as a single Eval-Console-PR1 group; the *spirit* of Anchor 3 is preserved because no sub-PR ships an "overclaim" alone — the registry lands before the runner that depends on it, before the web that exposes it.
4. **No failure inspector, no `suggested_fix` in PR1.** Frontend shows a placeholder "Failure inspector — coming in PR2; requires CausalFailureSliceExtractor wiring." `suggested_fix` UI rule: render only if `failure_slice.suggested_fix is not None`.
5. **Synthetic = first-class badge.** Every synthetic case row shows a `SYNTHETIC` badge; event-detail expansion exposes raw `Event` fields (`payload_kind`, `subject_class`, `sensitivity`, `retention_policy_id`).

## Open questions — adopted leans

- **OQ-1**: `/eval/runs` reads from disk (`<blob_dir>/eval_reports/`). Survives restarts; comparable across sessions.
- **OQ-2**: `POST /eval/runs` returns immediately with `{run_id, status: "started"}`. Live progress streams over a WebSocket (`/eval/ws/runs/{run_id}`). For PR1, synchronous polling on `GET /eval/runs/{run_id}` is sufficient if WebSocket adds risk — fall back to polling-only behind a feature flag if the WS path slips.
- **OQ-3**: Reports/output dir: `<blob_dir>/eval_reports/`, with `--eval-reports-dir` CLI flag override.
- **OQ-4**: Cancellation: explicit `POST /eval/runs/{run_id}/cancel` only. No auto-cancel on browser disconnect.
- **OQ-5**: Import direction: runner imports registry; registry imports adapter modules; adapter modules do not import registry. One-way.

---

## Sub-PR split with file-conflict map

| Sub-PR | Files touched (new = N, modified = M) | Depends on | Can run in parallel with |
|---|---|---|---|
| **E1: AdapterInfo registry** | N `companion_harness/evals/registry.py` | — | — (foundation) |
| **E2: Runner dispatch via registry** | M `companion_harness/evals/runners.py` | E1 | E3 |
| **E3: `/eval/*` route handlers** | N `manual_test_console/eval_routes.py` | E1 | E2 |
| **E4: server.py route registration** | M `manual_test_console/server.py` | E3 | — (small surgical edit; can land after E3) |
| **E5: Eval frontend HTML/JS/CSS + Live/Eval switcher** | N `manual_test_console/eval.html`, N `manual_test_console/eval_app.js`, N `manual_test_console/eval_style.css`, M `manual_test_console/index.html` | E3 (for endpoint contracts) | E2, E4 |
| **E6: Tests** | N `tests/test_eval_registry.py`, N `tests/test_eval_routes.py`, N `tests/test_eval_console_html_renders.py` | E1+E2+E3+E4+E5 | — |

### File-conflict analysis

- **No two sub-PRs modify the same file** except where ordered: E2 alone touches `runners.py`; E4 alone touches `server.py`; E5 alone touches `index.html`. All other touched files are new.
- **Parallel-safe wave structure**:
  - **Wave A (serial)**: E1.
  - **Wave B (parallel)**: E2, E3 fire concurrently after E1 lands. E2 touches `runners.py`; E3 creates `eval_routes.py`; zero file overlap.
  - **Wave C (parallel)**: E4, E5 fire concurrently after E3 lands. E4 modifies `server.py`; E5 creates frontend files + touches `index.html`; zero overlap.
  - **Wave D (serial)**: E6 after all of E1–E5 are on the integration branch.

### Branching convention

Each sub-PR uses an `eval-console-pr1/Ek-<slug>` branch off `main`:

- `eval-console-pr1/E1-registry`
- `eval-console-pr1/E2-runner-dispatch`
- `eval-console-pr1/E3-eval-routes`
- `eval-console-pr1/E4-server-wire`
- `eval-console-pr1/E5-frontend`
- `eval-console-pr1/E6-tests`

E2/E3 are based on E1 once it merges; E4/E5 on E3; E6 on E5.

---

## Tasks

### E1 — AdapterInfo registry

**File (new)**: `companion_harness/evals/registry.py`

**Contents**:

```python
from __future__ import annotations
from dataclasses import dataclass
from typing import Callable, Literal

from companion_harness.evals.protocols import CaseSource, BenchmarkAdapter


@dataclass(frozen=True)
class AdapterInfo:
    name: str
    version: str
    status: Literal["ready", "synthetic_only", "disabled"]
    case_count: int | None
    supports_real_mode: bool
    supports_synthetic_mode: bool
    build: Callable[[], BenchmarkAdapter]
    notes: str | None = None


def _build_harness_native() -> BenchmarkAdapter:
    from companion_harness.evals.adapters import harness_native
    import pathlib
    repo_root = pathlib.Path(__file__).parent.parent.parent
    return harness_native.build(repo_root=repo_root)


def _build_vocalbench() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.vocalbench import build_vocalbench
    return build_vocalbench(synthetic=True)


def _build_voicebench() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.voicebench import build_voicebench
    return build_voicebench(synthetic=True)


def _build_humdial_fdbench() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.humdial_fdbench import build_humdial_fdbench
    return build_humdial_fdbench(synthetic=True)


def _build_candor() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.candor import build
    return build(synthetic=True)


def _build_full_duplex_bench_v1() -> BenchmarkAdapter:
    from companion_harness.evals.adapters.full_duplex_bench import build_v1
    return build_v1()


ADAPTERS: dict[str, AdapterInfo] = {
    "harness_native":        AdapterInfo("harness_native", "v1", "ready",            None, True,  False, _build_harness_native),
    "vocalbench":            AdapterInfo("vocalbench", "synthetic-v1", "synthetic_only", None, False, True,  _build_vocalbench,            notes="real ingestion requires datasets extra"),
    "voicebench":            AdapterInfo("voicebench", "synthetic-v1", "synthetic_only", None, False, True,  _build_voicebench,            notes="real ingestion deferred"),
    "humdial_fdbench":       AdapterInfo("humdial_fdbench", "synthetic-v1", "synthetic_only", None, False, True,  _build_humdial_fdbench, notes="real ingestion deferred"),
    "candor":                AdapterInfo("candor", "synthetic-v1", "synthetic_only", None, False, True,  _build_candor,                notes="real CANDOR data deferred"),
    "full_duplex_bench":     AdapterInfo("full_duplex_bench", "v1", "synthetic_only", None, False, True,  _build_full_duplex_bench_v1, notes="real FDB data deferred"),
}
```

**Discipline notes**:
- Builders are lazy (`from ... import ...` inside the function) so importing `registry` does not trigger every adapter's transitive imports.
- `case_count` left `None`; later PRs may populate via a lazy probe.
- No CLI invocation here — registry is data only.

**Success criterion (E1)**: `from companion_harness.evals.registry import ADAPTERS; assert set(ADAPTERS) == {"harness_native", "vocalbench", "voicebench", "humdial_fdbench", "candor", "full_duplex_bench"}` passes; every builder is invocable and returns a `BenchmarkAdapter`.

---

### E2 — Runner dispatch via registry

**File (modified)**: `companion_harness/evals/runners.py`

**Change**: Replace the hardcoded `if args.adapter != "harness_native"` check with registry lookup.

```python
def main(argv: list[str] | None = None) -> int:
    # ... existing arg parsing ...
    if args.command == "run":
        from companion_harness.evals.registry import ADAPTERS
        info = ADAPTERS.get(args.adapter)
        if info is None:
            print(
                f"adapter '{args.adapter}' is not registered. "
                f"Known: {sorted(ADAPTERS)}. "
                "File new-adapter requests via `gh issue create --label eval-adapter`.",
                file=sys.stderr,
            )
            return 2
        if info.status == "disabled":
            print(f"adapter '{args.adapter}' is disabled: {info.notes or ''}", file=sys.stderr)
            return 2
        return _run_adapter(info, args.output, args.split)
```

`_run_adapter(info, output, split)` is a generalization of `_run_harness_native` that calls `info.build()` instead of hardcoded `harness_native.build(...)`. Preserve the run_id pattern (`{name_prefix}-{ts}-{uuid6}`) where `name_prefix` is the first letters of `info.name`.

**Risk mitigation (per draft Risk 1)**: keep `_run_harness_native` as an internal alias for one cycle so any test/script that imported it directly does not break. Removal scheduled for PR2.

**Discipline notes**:
- Do NOT change CLI surface beyond the dispatch error message. `--adapter`, `--timing-mode`, `--output`, `--split` all stay.
- Synthetic adapters expose async `run(...)` per the `ScenarioDriver` Protocol. `_run_adapter` must call `await scenario_driver.run(...)` for all adapters except `harness_native`, which retains its `_run_sync` path. If a synthetic adapter's driver lacks `run`, E2 stops at that adapter with a clear error rather than silently degrading. Document in PR body.

**Success criterion (E2)**: `python -m companion_harness.evals run --adapter vocalbench --output /tmp/r` exits 0 and writes a `run.json`; `--adapter does_not_exist` exits 2 with the `gh issue` hint.

---

### E3 — `/eval/*` route handlers

**File (new)**: `manual_test_console/eval_routes.py`

**Handlers** (all aiohttp `web.Request → web.Response`):

- `GET /eval` — serve `manual_test_console/eval.html` (static file).
- `GET /eval/static/{name}` — serve static assets from `manual_test_console/` by filename (e.g. `eval_app.js`, `eval_style.css`). Use aiohttp `web.static` or a minimal handler that resolves to the same directory as `eval.html`. Asset paths: `/eval/static/eval_app.js`, `/eval/static/eval_style.css`.
- `GET /eval/adapters` — JSON: `[{name, version, status, case_count, supports_real_mode, supports_synthetic_mode, notes}, ...]` from `ADAPTERS`.
- `GET /eval/runs` — list `<blob_dir>/eval_reports/*/run.json`. Return `[{run_id, adapter, started_at, finished_at, status, case_count, pass_count}]`.
- `POST /eval/runs` — body `{adapter, split?}`. Launch run in background task (asyncio.create_task wrapping a `loop.run_in_executor(...)` of `runners._run_adapter`). Return `{run_id, status: "started"}` immediately. Store handle in `app[KEY_EVAL_RUNS]` for cancellation.
- `GET /eval/runs/{run_id}` — return manifest + metrics from disk (`run.json` + any aggregated metrics).
- `GET /eval/runs/{run_id}/event_logs/{case_id}` — stream JSONL from `<blob_dir>/eval_reports/<run_id>/<case_id>/events.jsonl` (or wherever the adapter wrote it; honor `replay_run.event_log_path`).
- `POST /eval/runs/{run_id}/cancel` — cancel the asyncio task; mark run status as `cancelled` in an in-memory map (disk run.json is updated on finalization).

**AppKey additions**:

```python
KEY_EVAL_REPORTS_DIR: web.AppKey[Path] = web.AppKey("eval_reports_dir", Path)
KEY_EVAL_RUNS: web.AppKey[dict[str, "EvalRunHandle"]] = web.AppKey("eval_runs", dict)
```

`EvalRunHandle` is a small dataclass: `{run_id, adapter, task, status, started_at}`. Lives in `eval_routes.py`.

**Function**: `register_eval_routes(app: web.Application, *, eval_reports_dir: Path) -> None` that registers all the above and seeds the AppKeys. Called from `server.py` (E4).

**Discipline notes**:
- All handlers use `app[KEY_LOGGER]` for any side-effect events if needed (e.g. `eval_run_started`). Optional in PR1; the core requirement is just artifact serving.
- `GET /eval/runs/{run_id}/event_logs/{case_id}` uses `web.FileResponse` if the file exists, else 404 with a structured JSON error.
- WebSocket for live progress: **omitted in PR1** (sync polling via `GET /eval/runs/{run_id}` is sufficient). No WS stub is registered. The polling approach is the settled choice for PR1; WS is deferred to PR2 alongside the failure inspector.

**Success criterion (E3)**: `eval_routes.py` imports without side effects; `register_eval_routes` adds 7 routes (6 REST handlers + 1 static-asset route, no WebSocket); aiohttp test client can hit each route against a stub blob_dir.

---

### E4 — server.py route registration

**File (modified)**: `manual_test_console/server.py`

**Surgical edit** (single block inside `build_app`):

```python
    app.router.add_post("/config/reset", _handle_post_config_reset)

    # --- Eval console PR1 (E4) ---
    from manual_test_console.eval_routes import register_eval_routes
    eval_reports_dir = blob_dir / "eval_reports"
    eval_reports_dir.mkdir(parents=True, exist_ok=True)
    register_eval_routes(app, eval_reports_dir=eval_reports_dir)
```

Also add `eval_reports_dir: Path | None = None` to `build_app` kwargs (defaulting to `blob_dir / "eval_reports"` when None) and a CLI `--eval-reports-dir` flag in `main()` per OQ-3.

**Discipline notes**:
- Do NOT touch any other route registration or AppKey wiring. Surgical only.
- The import is local-to-function to avoid loading the eval subsystem at module import time (mirrors how `companion_harness` is treated elsewhere in this file).

**Success criterion (E4)**: `pytest tests/test_eval_routes.py` (from E6) sees the routes registered after `build_app(blob_dir=tmp_path)`.

---

### E5 — Eval frontend HTML/JS/CSS + Live/Eval switcher

**Files (new)**:
- `manual_test_console/eval.html`
- `manual_test_console/eval_app.js`
- `manual_test_console/eval_style.css`

**File (modified)**:
- `manual_test_console/index.html` — add `[Live] [Eval]` switcher to the header.

**Components in `eval.html`** (per draft Anchor 5):
- `HeaderBar` — title, `[Live] [Eval]` switcher (Eval active), connection status. Reuses `index.html`'s dark/monospace style vocabulary (`--bg`, `--fg`, `--accent`, monospace font).
- `AdapterPanel` — dropdown sourced from `GET /eval/adapters`; each entry shows `{name} {version} [{status}]`; `synthetic_only` entries get a `SYNTHETIC` badge. `Run` button POSTs to `/eval/runs`.
- `RunListPanel` — table of runs from `GET /eval/runs`. Columns: run_id, adapter, started_at, status, pass/total. Click → load detail.
- `RunDetailPanel`:
  - `RunSummary` — adapter, run_id, status, case counts.
  - `CaseList` — one row per case. Each row shows case_id, final_status, `SYNTHETIC` badge if applicable, and an expand toggle.
  - `EventLogTabs` — per-case `events.jsonl` viewer. Each event row shows kind + summary; expansion exposes raw schema fields (`payload_kind`, `subject_class`, `sensitivity`, `retention_policy_id`).
  - `ArtifactLinks` — links to raw `run.json`, `events.jsonl` per case.
- `FailureInspectorPlaceholder` — fixed text: `Failure inspector — coming in PR2; requires CausalFailureSliceExtractor wiring.` Renders as a muted panel; **no input controls**.

**Switcher in `index.html`** — add inside the existing `<header>`:

```html
<nav class="page-switcher">
  <a href="/" class="active">Live</a>
  <a href="/eval">Eval</a>
</nav>
```

`eval.html` mirrors the same nav with `Eval` marked active.

**`eval_app.js`** — vanilla JS, no build step. Mirrors `index.html`'s pattern (inline `<script>` or referenced module). Uses `fetch` for REST + optional `WebSocket` for live run progress.

**`eval_style.css`** — eval-only stylesheet. Does NOT extract or modify inline CSS from `index.html` (Live view keeps its inline styles as-is). Defines eval-specific layout and the `.badge.synthetic` class. If shared design tokens (e.g. `--bg`, `--fg`, `--accent`) are needed, they are duplicated here rather than refactored out of `index.html` — scope is narrower that way.

**Discipline notes**:
- No new JS framework. Match `index.html`'s style.
- Synthetic badge is a class-styled `<span>` — visible regardless of adapter dropdown state (per draft Anchor 5).
- `suggested_fix` is NOT rendered anywhere in this PR (per draft Anchor 4).

**Success criterion (E5)**: opening `http://127.0.0.1:8800/eval` in a browser shows the page; switcher navigates back to `/`; `eval_app.js` populates the adapters dropdown from `/eval/adapters`.

---

### E6 — Tests

**Files (new)**:

#### `tests/test_eval_registry.py` (≥ 4 tests)
- `test_all_six_adapters_present` — `ADAPTERS` has exactly the 6 expected keys.
- `test_each_adapter_info_is_well_formed` — every entry has non-empty name/version, status in allowed literal set, exactly one of supports_real_mode/supports_synthetic_mode true (or both for ready adapters).
- `test_each_builder_returns_benchmark_adapter` — call `info.build()` for each; assert it has `.case_source` and `.scenario_driver`.
- `test_registry_does_not_import_runtime` — re-uses or mirrors `tests/test_runtime_does_not_import_evals.py`'s import-direction style to assert `companion_harness.evals.registry` does not transitively pull `companion_harness.realtime_loop` etc.

#### `tests/test_eval_routes.py` (≥ 6 tests, using `aiohttp_client` fixture)
- `test_get_eval_adapters_returns_six_entries`
- `test_get_eval_runs_lists_disk_runs` — seed `<blob_dir>/eval_reports/run-x/run.json`; assert listed.
- `test_post_eval_runs_returns_run_id_and_started_status` — body `{adapter: "harness_native"}` → `{run_id, status: "started"}`.
- `test_get_eval_runs_run_id_returns_manifest` — write a fixture run.json; assert handler returns its contents.
- `test_get_event_log_streams_jsonl` — write a fixture events.jsonl; assert handler returns bytes-equal content.
- `test_post_cancel_marks_run_cancelled` — launch run, cancel, assert subsequent GET shows `status: cancelled`.
- `test_unknown_adapter_post_returns_400` — bad name → 400 with structured error containing `gh issue` hint.

#### `tests/test_eval_console_html_renders.py` (≥ 2 tests)
- `test_eval_html_serves_at_eval_route` — `GET /eval` returns 200, content-type `text/html`, body contains `id="adapter-panel"` and `Failure inspector — coming in PR2`.
- `test_index_html_contains_eval_switcher` — `GET /` body contains `href="/eval"`.

#### Additional tests covering gaps (add to `tests/test_eval_routes.py` or a new file)
- `test_get_eval_static_js` — `GET /eval/static/eval_app.js` returns 200 with `Content-Type: application/javascript`.
- `test_get_eval_static_css` — `GET /eval/static/eval_style.css` returns 200 with `Content-Type: text/css`.
- `test_post_eval_runs_all_six_adapters_dispatch` — POST `/eval/runs` for each of the 6 registry keys; assert each returns `{run_id, status: "started"}` (no 400/500). Uses mocked `_run_adapter` so no real execution occurs.
- `test_eval_reports_dir_cli_flag` — invoke `build_app` with a custom `eval_reports_dir`; assert `app[KEY_EVAL_REPORTS_DIR]` matches the passed path (validates the `--eval-reports-dir` wiring).

**Total ≥ 16 tests** (per numeric gate).

**Discipline notes**:
- Tests use the existing `aiohttp.test_utils.TestClient` pattern (already used by other manual_test_console tests; copy the conftest).
- No real adapter execution in tests — POST `/eval/runs` uses `harness_native` (which is the fastest synthetic case set already shipped) and the test awaits completion or cancellation.
- All tests run under the canonical venv `/raid/yid042/venvs/companion-harness` (per project memory).

**Success criterion (E6)**: `pytest -k "eval_registry or eval_routes or eval_console_html"` reports ≥ 16 passing, 0 failing.

---

## Numeric gates (PR1 group)

| Metric | Gate | Verified by |
|---|---|---|
| `eval_adapter_dispatch_coverage` | == 1.0 (all 6 dispatchable from web) | E6 `test_get_eval_adapters_returns_six_entries` + manual POST per adapter |
| `eval_route_request_latency_ms_p95` | < 100 ms (excluding run wall time) | E6 manual timing or simple route latency test |
| `eval_html_renders_without_js_errors` | true | Manual smoke (handbook entry) + `test_eval_html_serves_at_eval_route` |
| Targeted new test count | ≥ 16 | E6 file count |

## Risks (carried from draft + execution-specific)

1. **Registry-runner coupling regression** — E2 alone could break `harness_native`-only callers. Mitigation: keep `_run_harness_native` as internal alias for one cycle.
2. **`reports/` disk growth** — defer retention to v0.2 Wave 6. Document manual cleanup in PR body.
3. **WebSocket complexity** — WebSocket is explicitly out of PR1 scope (settled in WARN 6 lean). Synchronous polling via `GET /eval/runs/{run_id}` is the only status mechanism in PR1. WS deferred to PR2.
4. **Sub-PR review-order skew** — if E2 lands before E1 by accident (e.g., reviewer haste), the runner breaks on import. Mitigation: branch name prefix `Ek-` makes the order obvious; CI on the integration branch runs E6 last.
5. **`build_v1` vs `build_v1_5` for full_duplex_bench** — PR1 registers `full_duplex_bench_v1` only. v1_5 deferred.

## Out of scope (deferred per draft)

- PR2: Failure inspector + `CausalFailureSliceExtractor` wiring.
- PR3: Report/artifact polish (report.md preview, download bundle).
- PR4: Tier-B replay controls + policy-decision diff view.
- PR5+: Compare runs.

## Convergence note

This execution plan does not re-litigate any of the 5 anchors or 5 OQ leans from the draft. It only translates the 6-task scope into 6 parallel-safe sub-PRs with explicit file boundaries. Sub-PR coders should read the draft for "why" decisions and this document for "what to ship".
