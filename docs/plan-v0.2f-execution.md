# Plan — v0.2f Execution (Deployability capability pack + v0.2-final tag)

## Status: **DRAFT** — drafted 2026-05-16. Source roadmap: `docs/roadmap-v0.2-draft.md` (Wave 6, Tasks 17–20). Not yet plan-critic'd; not yet sub-agent dispatched.

> v0.2f is the **final** v0.2 sub-stage. It hardens the runtime *capabilities* that an external operator needs to host the harness without manual workarounds — deterministic replay-report export, opt-in session-blob retention, and `/healthz` extensions for external monitors — then performs the milestone-tag housekeeping (`POLICY_VERSION v0.1k → v0.2-final`, `scripts/v0_2_replay_report.py`, `git tag v0.2`).
>
> Per **Anchor 5** of the v0.2 roadmap, this sub-stage ships *capabilities only*. No Docker, systemd, k8s, secrets management, or log-shipping work lives here — those are v0.3 scope. If a task in this plan tempts a Dockerfile or systemd unit, that's a signal to stop and re-scope.

---

## §0 Scope discipline (the load-bearing constraint)

The Wave 6 capability surface is intentionally small. Three concrete capability deltas, one POLICY_VERSION bump, one git tag:

- **T1** Replay-report export hardening — deterministic file paths + structured JSON manifest.
- **T2** Blob-export tar generator — package a session's replay report + raw blobs into a reproducible tarball.
- **T3** `--blob-retention-days N` flag + rotation worker — opt-in, defaults preserve current behavior.
- **T4** `/healthz` per-adapter readiness extension.
- **T5** `/healthz` GPU memory budget reporting.
- **T6** `/healthz` event-rate counters (last-60s window).
- **T7** `POLICY_VERSION v0.1k → v0.2-final` bump + fixture migration sweep + `scripts/v0_2_replay_report.py`.
- **T8** `git tag v0.2` — operator-only step.

What this plan deliberately does NOT touch:

- Dockerfile, docker-compose, k8s manifests, Helm charts. (v0.3.)
- systemd / launchd / supervisord units. (v0.3.)
- Secret managers (Vault, AWS Secrets Manager, dotenv). (v0.3.)
- Log shipping (Vector, Fluent Bit, OpenTelemetry exporters). (v0.3.)
- Multi-host orchestration, leader election, replication. (v0.3+.)
- New event types or new policy inputs. (Spec FROZEN; v0.2-final bump is fixture-migration + script mirror only, NOT a behavioral change.)

A reviewer who sees any of the above in a v0.2f PR should reject and route to v0.3 scope.

---

## §Dependency graph

```
┌──────────────────────────────────────────────────────────────────────┐
│ v0.2f is the LAST v0.2 sub-stage.                                    │
│ ALL of v0.2a / v0.2b / v0.2c / v0.2d / v0.2e must merge before T1.   │
│ Concretely: v0.1k POLICY_VERSION (from Wave 2 / v0.2b) MUST be live  │
│ on main before T7's v0.1k → v0.2-final bump.                         │
└────────────┬─────────────────────────────────────────────────────────┘
             │
   ┌─────────┼──────────────────────────────┬────────────────────────┐
   │         │                              │                        │
   ▼         ▼                              ▼                        ▼
 Wave A (replay-export, 2x parallel)   Wave B (blob retention, 1x)   Wave C (/healthz, 3x parallel)
   T1 export hardening                   T3 --blob-retention-days     T4 per-adapter readiness
   T2 blob-export tar generator                                       T5 GPU memory budget
                                                                      T6 event-rate counters
             │                                  │                          │
             └────────────────┬─────────────────┴──────────────────────────┘
                              ▼
                     Wave D (POLICY_VERSION + tag, sequential)
                       T7 v0.1k → v0.2-final + fixture sweep + v0_2_replay_report.py
                       T8 git tag v0.2 (operator-only)
```

---

## §Concurrency map

| Wave | Concurrency | Tasks | Notes |
|---|---|---|---|
| A | **2×** | T1, T2 | T2 reuses T1's manifest shape; if T1 in flight, T2 author should align manifest fields. Lean: T1 lands first to set the manifest contract, T2 follows. |
| B | **1×** | T3 | Standalone CLI flag + rotation worker. No coupling to Wave A or C. |
| C | **3×** | T4, T5, T6 | All extend the existing `_handle_health` JSON. Conflicts are textual (same function body); coordinator should serialize merges OR ship as one PR. Lean: one PR for all three to avoid rebase tax. |
| D | sequential | T7 → T8 | T7 lands last among code PRs. T8 is operator-only (git tag); no PR. |

**Max realistic concurrency: 3 agents** (Wave A × 2 + Wave B × 1, OR Wave C × 3 as one combined PR). Realistic peak: 4 PRs in flight if Wave C is split.

**Foundation gate:** all of v0.2a–v0.2e must be on main. Specifically the v0.1k POLICY_VERSION bump (from v0.2b / Wave 2 Task 9) must be live; T7 assumes the current literal is `"v0.1k"`.

---

## §Cross-milestone coordination

- **Anchor 5 (capability-only).** Every PR description in v0.2f must explicitly state "capability-only per v0.2 Anchor 5; deployment posture deferred to v0.3." A reviewer who sees Docker/systemd/k8s artifacts rejects on Anchor 5.
- **POLICY_VERSION two-step cadence (Anchor 1).** v0.1k → v0.2-final is the *final* bump in the cadence. Every fixture pinning `policy_version` must be migrated in T7's PR (the same way v0.1j Task 18 migrated v0.1d → v0.1j in one PR).
- **Replay-report mirror (Anchor 1 implication).** `scripts/v0_2_replay_report.py` mirrors the v0.1j pattern (`scripts/v0_1h_replay_report.py` is the most recent template). This script is the auditable artifact for the v0.2 milestone — every Wave's gate appears as a line in the report.
- **`/healthz` schema compatibility.** External monitors (none yet, but the operator may add one between v0.2f land and v0.3) consume `_handle_health`. The Wave C extensions ADD fields; never rename or remove existing fields. JSON additive contract.

---

# TASKS

---

## Task 1 — Replay-report export hardening (deterministic paths + JSON manifest)

**Files touched**
- `companion_harness/replay.py` — currently a 7-line module docstring stub. Add an `export_replay_report(session_id, out_dir)` function that walks the EventLogger's persisted JSONL, emits deterministic file paths (`{out_dir}/{session_id}/events.jsonl`, `{out_dir}/{session_id}/manifest.json`, `{out_dir}/{session_id}/blobs/`), and writes a structured JSON manifest.
- `tests/test_replay_report_export.py` — NEW. Contract test: given a fixture session log, `export_replay_report()` produces a deterministic directory tree whose file paths and manifest JSON match a golden snapshot byte-for-byte.

**Implementation sketch**
1. Define `ReplayReportManifest` shape (lean: dataclass in `companion_harness/replay.py`):
   - `session_id: str`
   - `policy_version: str` (read from `companion_harness.speak_policy.POLICY_VERSION`)
   - `schema_version: str` (read from any event in the log; assert all equal)
   - `event_count: int`
   - `event_count_by_type: dict[str, int]`
   - `first_event_timestamp_mono_ms: int`
   - `last_event_timestamp_mono_ms: int`
   - `blob_refs: list[str]` (`payload_ref` values, sorted)
   - `manifest_schema_version: str` literal `"replay-report-manifest/v1"`
   - `generated_at_wall: str` (ISO 8601 UTC; the ONLY non-deterministic field — segregated so callers can normalize for snapshot tests)
2. `export_replay_report(session_id, source_log_path, out_dir, *, blob_source_dir)`:
   - Reads `source_log_path` (JSONL), filters to `event.session_id == session_id`.
   - Writes events to `{out_dir}/{session_id}/events.jsonl` preserving original line order (do NOT re-serialize; copy lines verbatim).
   - Copies each `payload_ref` blob from `blob_source_dir` into `{out_dir}/{session_id}/blobs/` (filename = basename of `payload_ref`).
   - Writes `manifest.json` (sorted keys, two-space indent — match `json.dumps(..., sort_keys=True, indent=2)`).
3. Contract test fixture: a 5-event log + 1 blob, golden tree under `tests/fixtures/replay_report_export/golden/`. Test asserts directory contents match byte-for-byte EXCEPT `generated_at_wall` (which the test overwrites to a fixed value before comparison).
4. No CLI yet — that's the operator's wrapper. `export_replay_report()` is callable from `python -c` or a future subcommand.

**Test plan**
- `pytest tests/test_replay_report_export.py -q` under canonical venv passes.
- Run twice on the same input; output is bit-identical (modulo `generated_at_wall` which the test normalizes).

**Success criterion**
```
pytest tests/test_replay_report_export.py -q  # under canonical venv, all green
```

**Anchors locked** — Anchor 5 (capability-only, no Docker/systemd).

**OQs (pre-resolved leans)**
- OQ-1a: Manifest format JSON or YAML? **Lean: JSON** — matches event log format; no new dependency; `json.dumps(sort_keys=True, indent=2)` gives deterministic output for free.
- OQ-1b: Copy blobs or symlink? **Lean: COPY** — symlinks break the tar generator (T2) on portability; copy is the safe default. T3's retention worker won't touch exported copies.
- OQ-1c: Include the `generated_at_wall` field at all? **Lean: YES, segregated** — operators want to know when the export ran; tests normalize the field for snapshot comparison.

**Cross-references** — `companion_harness/replay.py` (current stub); `companion_harness/event_logger.py` (sink format); roadmap-v0.2-draft.md Wave 6 Task 17.

**Blocker dependencies** — all of v0.2a–v0.2e merged.

---

## Task 2 — Blob-export tar generator

**Files touched**
- `companion_harness/replay.py` — add `export_replay_report_tar(session_id, source_log_path, out_path, *, blob_source_dir)` that wraps `export_replay_report` and packages the output directory into a deterministic tar archive at `out_path`.
- `tests/test_replay_report_tar.py` — NEW. Contract test: tar member names and sizes match a golden manifest; tar is byte-stable across runs (modulo `generated_at_wall`).

**Implementation sketch**
1. Reuse `export_replay_report` (T1) by writing to a `tempfile.TemporaryDirectory()`, then `tarfile.open(out_path, "w")` and `add()` the tree with a deterministic walk order (sorted by name).
2. Set deterministic tarinfo attributes: `uid=0`, `gid=0`, `uname=""`, `gname=""`, `mtime=0`. (This guarantees byte-stable archive content across machines and runs.)
3. Tar format: **plain `.tar`** (uncompressed). Operators compress externally if needed.
   - Rationale: byte-stable across runs is easier without compression (gzip embeds a wall-clock by default unless caller sets `mtime=0` on the gzip header).
   - Reversal cost: low — adding `.tar.gz` later is one parameter.
4. No CLI subcommand yet (operator wraps with `python -c` or a one-liner script). Adding a CLI is out of scope for v0.2f per Anchor 5.

**Test plan**
- `pytest tests/test_replay_report_tar.py -q` under canonical venv passes.
- `tarfile.open(out_path).getnames()` returns the expected sorted list.
- Two consecutive runs produce identical tar bytes (after normalizing `generated_at_wall` in the manifest via re-writing the inner manifest file before re-comparing).

**Success criterion**
```
pytest tests/test_replay_report_tar.py -q  # under canonical venv, all green
python -c "import tarfile; print(sorted(tarfile.open('/tmp/foo.tar').getnames()))"  # matches golden
```

**Anchors locked** — Anchor 5 (capability-only). Anchor: tar (not zip) per OQ-2a.

**OQs (pre-resolved leans)**
- OQ-2a: tar vs zip vs tar.gz? **Lean: plain tar** — byte-stable without extra effort; standard on every operator's box; compression is the operator's choice. tar.gz reversible behind a flag if requested.
- OQ-2b: Include a CLI subcommand (`python -m companion_harness.replay export ...`)? **Lean: NO for v0.2f** — capability-only per Anchor 5. The Python function suffices; the operator wraps. CLI surface decision lives in v0.3.

**Cross-references** — T1 (manifest contract); roadmap-v0.2-draft.md Wave 6 Task 17.

**Blocker dependencies** — T1 merged (manifest shape is the contract).

---

## Task 3 — `--blob-retention-days N` flag + rotation worker

**Files touched**
- `manual_test_console/server.py` — add `--blob-retention-days N` CLI argument (type `int`, default `30`); wire into a background asyncio task that scans `blob_dir` for files with `mtime` older than the window and unlinks them.
- `companion_harness/event_logger.py` — add a comment in the module docstring cross-referencing `--blob-retention-days` as the retention surface for blob payloads (event-log retention is governed elsewhere; this is the blob-store surface).
- `tests/test_blob_retention_rotation.py` — NEW. Contract test: given a `blob_dir` populated with files of varying `mtime`s, the rotation worker removes only files older than the window and leaves the rest intact.

**Implementation sketch**
1. CLI: `parser.add_argument("--blob-retention-days", type=int, default=30, help="Delete blob files older than N days. Default 30. Set to 0 to disable rotation.")`.
2. If `args.blob_retention_days > 0`: start an asyncio task in `build_app` that wakes every `min(3600, retention_seconds // 24)` seconds, walks `blob_dir`, and `Path.unlink()`s files with `mtime` older than `now - retention_days * 86400`.
3. Rotation worker is **idempotent**, **single-pass per wake**, and **never blocks the realtime path** (per invariant #10 — same async-friendly posture).
4. On wake, emit a `log_drop_or_degrade`-style operator-action event? **Lean: NO** — rotation is operator policy, not signal degradation. A simple log line via the existing print path is enough; richer telemetry lives in v0.3.
5. Contract test uses `os.utime()` to backdate file mtimes; asserts the post-rotation set matches expectations.

**Test plan**
- `pytest tests/test_blob_retention_rotation.py -q` under canonical venv passes.
- Manual smoke: `--blob-retention-days 0` disables the worker; `--blob-retention-days 1` removes day-old files after one tick.

**Success criterion**
```
pytest tests/test_blob_retention_rotation.py -q  # under canonical venv, all green
python -m manual_test_console.server --help | grep -- "--blob-retention-days"  # flag visible
```

**Anchors locked** — Anchor 5 (capability-only; no log shipping, no external rotation service).

**OQs (pre-resolved leans)**
- OQ-3a: Default retention days? **Lean: 30** — matches existing event-retention conventions referenced in `event_logger.py` cross-references; long enough for most replay-report use cases; short enough to bound disk growth.
- OQ-3b: Rotation tick interval? **Lean: hourly** — fine-grained enough that operator's `--blob-retention-days 1` works within 60 minutes; cheap enough that disk I/O is negligible.
- OQ-3c: Emit per-deletion events? **Lean: NO** — rotation is operator policy, not a replay-affecting signal. Avoid polluting the event log with noise. A single startup print stating the retention policy suffices.
- OQ-3d: Include a `--dry-run` mode? **Lean: NO for v0.2f** — operator can set `--blob-retention-days 0` to disable. Dry-run is v0.3 polish.

**Cross-references** — `manual_test_console/server.py:1049-1053` (existing `--blob-dir` flag); roadmap-v0.2-draft.md Wave 6 Task 18.

**Blocker dependencies** — all of v0.2a–v0.2e merged.

---

## Task 4 — `/healthz` per-adapter readiness extension

**Files touched**
- `manual_test_console/server.py` — extend `_handle_health` (line 525) to add per-adapter readiness keys: `vad_ready`, `smart_turn_ready`, `backchannel_ready`, `asr_ready`, `tts_ready`, `vision_ready`, `foreground_model_ready`. Each is `True` iff the adapter is loaded and has not raised on its most recent call.
- `tests/test_healthz_per_adapter_readiness.py` — NEW. Contract test: hit `/healthz` against a `build_app(...)` instance with mocked factories; assert each `*_ready` key is `True` when the adapter is loaded, `False` when it's `None`.

**Implementation sketch**
1. The existing `_handle_health` already exposes adapter labels (`vad_model`, `smart_turn_model`, etc.). Extend with boolean readiness derived from factory output: an adapter is "ready" iff its `KEY_*` value is not `None` (loaded successfully).
2. For per-call health (has the adapter raised recently?): out of scope for v0.2f. Lean: bool-from-loaded suffices. A future v0.3 task can add rolling failure-rate tracking.
3. The `/healthz` response stays **additive** — no existing key is renamed or removed.
4. Test asserts:
   - With all factories provided: every `*_ready` is `True`.
   - With factory set to a `lambda: None`: corresponding `*_ready` is `False`.
   - With `--no-live-pipeline`: all `*_ready` are `False`.

**Test plan**
- `pytest tests/test_healthz_per_adapter_readiness.py -q` under canonical venv passes.

**Success criterion**
```
pytest tests/test_healthz_per_adapter_readiness.py -q  # under canonical venv, all green
curl -s http://localhost:8800/healthz | jq '.vad_ready, .asr_ready, .tts_ready'  # returns booleans
```

**Anchors locked** — Anchor 5 (capability-only; no external monitor wiring).

**OQs (pre-resolved leans)**
- OQ-4a: Rolling failure-rate per adapter, or bool-from-loaded? **Lean: bool-from-loaded** — simplest signal; failure-rate is v0.3 monitoring scope.
- OQ-4b: One PR for T4/T5/T6 or three? **Lean: ONE PR** — all three extend the same function body; serialized merges cost rebase tax. The coordinator's call.

**Cross-references** — `manual_test_console/server.py:525-566` (current `_handle_health`); roadmap-v0.2-draft.md Wave 6 Task 19.

**Blocker dependencies** — all of v0.2a–v0.2e merged.

---

## Task 5 — `/healthz` GPU memory budget reporting

**Files touched**
- `manual_test_console/server.py` — extend `_handle_health` with `gpu_memory_allocated_mb`, `gpu_memory_reserved_mb`, `gpu_memory_total_mb` keys. Source via `torch.cuda.mem_get_info()` and `torch.cuda.memory_allocated()` IF `torch.cuda.is_available()`, else all three keys are `None`.
- `tests/test_healthz_gpu_memory.py` — NEW. Contract test: assert the three keys exist; assert types are `int | None`; assert `None` when CUDA unavailable.

**Implementation sketch**
1. Import `torch` lazily inside `_handle_health` — the server should still respond on a non-torch environment.
2. Wrap the torch call in try/except; on any exception, set all three to `None` and continue.
3. Report MiB (mebibytes) for human readability; convert from bytes by `// (1024 * 1024)`.
4. Add a `gpu_device_name` string (e.g. `"NVIDIA H100"`) for operator clarity. Set to `None` when CUDA unavailable.
5. Test mocks `torch.cuda.is_available` to `False` and asserts all four keys are `None`.

**Test plan**
- `pytest tests/test_healthz_gpu_memory.py -q` under canonical venv passes.
- Manual smoke on b200: `curl -s http://localhost:8800/healthz | jq '.gpu_memory_allocated_mb, .gpu_device_name'` returns sensible numbers + device name.

**Success criterion**
```
pytest tests/test_healthz_gpu_memory.py -q  # under canonical venv, all green
```

**Anchors locked** — Anchor 5.

**OQs (pre-resolved leans)**
- OQ-5a: Per-process GPU memory or per-device? **Lean: per-device** (`mem_get_info()`) — simpler; matches what operators see in `nvidia-smi`.
- OQ-5b: Include per-adapter VRAM attribution? **Lean: NO for v0.2f** — torch doesn't give us a clean per-adapter breakdown without per-call hooks; that's v0.3 scope.
- OQ-5c: Report in MiB or bytes? **Lean: MiB** — human-readable, matches existing operator tooling (`nvidia-smi`).

**Cross-references** — roadmap-v0.2-draft.md Wave 6 Task 19; `manual_test_console/server.py:525` (current `_handle_health`).

**Blocker dependencies** — all of v0.2a–v0.2e merged.

---

## Task 6 — `/healthz` event-rate counters (last-60s window)

**Files touched**
- `manual_test_console/server.py` — extend `_handle_health` with `events_per_second_last_60s` (int rounded to 0 decimals) and `events_total_since_start` (int). Source via a thin EventLogger sink subscriber that maintains a deque of `(timestamp_mono_ms, event_count)` pairs trimmed to the last 60 seconds.
- `companion_harness/event_logger.py` — NO changes (existing `subscribe()` is the seam).
- `tests/test_healthz_event_rate.py` — NEW. Contract test: subscribe a fake counter; inject N events with controlled timestamps; assert the 60s window arithmetic matches.

**Implementation sketch**
1. Define a small `_EventRateCounter` class in `manual_test_console/server.py` (lean: same module — keep the surface tight; promote to its own module only if reused elsewhere):
   - `__init__(self, window_seconds: int = 60)`
   - `async def on_event(self, event: Event) -> None` — appends `event.timestamp_mono_ms` to an internal deque, trims entries older than `window_seconds * 1000` ms.
   - `def rate_per_second(self) -> float` — `len(deque) / window_seconds`.
   - `def total_count(self) -> int` — monotonic counter incremented on each `on_event`.
2. In `build_app`, instantiate one counter; subscribe via `logger.subscribe(counter.on_event)`.
3. `_handle_health` reads `counter.rate_per_second()` (rounded to int) and `counter.total_count()`.
4. Window is **fixed at 60s** (no flag) — matches the OQ lean below.
5. Contract test injects synthetic events with monotonically-increasing `timestamp_mono_ms`; asserts trim and rate math.

**Test plan**
- `pytest tests/test_healthz_event_rate.py -q` under canonical venv passes.
- Manual smoke: `curl -s http://localhost:8800/healthz | jq '.events_per_second_last_60s, .events_total_since_start'` shows non-decreasing total + reasonable rate.

**Success criterion**
```
pytest tests/test_healthz_event_rate.py -q  # under canonical venv, all green
```

**Anchors locked** — Anchor 5. Invariant #10 (subscriber must not block drain loop — exception-swallowed by EventLogger's `try/except`).

**OQs (pre-resolved leans)**
- OQ-6a: Window size? **Lean: 60s** — short enough to catch a stalled-pipeline blip; long enough to smooth across normal pauses.
- OQ-6b: Per-event-type breakdown? **Lean: NO for v0.2f** — total rate is the load signal; per-type breakdown is dashboard / Grafana scope (v0.3).
- OQ-6c: Float or rounded-int rate? **Lean: rounded int** — operator scans the value at a glance; sub-Hz precision is noise.

**Cross-references** — `companion_harness/event_logger.py:39-46` (existing subscribe seam); roadmap-v0.2-draft.md Wave 6 Task 19.

**Blocker dependencies** — all of v0.2a–v0.2e merged.

---

## Task 7 — POLICY_VERSION bump v0.1k → v0.2-final + fixture migration sweep + `scripts/v0_2_replay_report.py`

**Files touched**
- `companion_harness/speak_policy.py` — change the `POLICY_VERSION = "v0.1k"` literal (assumed live on main from v0.2b) to `POLICY_VERSION = "v0.2-final"`.
- `tests/fixtures/**` and `tests/**` — every fixture or assertion pinning `"v0.1k"` migrates to `"v0.2-final"`. Use `grep -rn '"v0.1k"' tests/` to enumerate; replace each occurrence one-by-one (per coding rule 3, surgical edits — no opportunistic refactor).
- `scripts/v0_2_replay_report.py` — NEW. Mirror the structure of `scripts/v0_1h_replay_report.py` (most recent template). Sections: header, per-wave gate report, POLICY_VERSION verification, milestone-tag readiness summary.
- `ROADMAP.md` — bump the CURRENT-MILESTONE pointer to v0.2-final (one-line edit).
- `README.md` — bump the milestone pointer if present (one-line edit).

**Implementation sketch**
1. **Pre-flight grep**: `grep -rn '"v0.1k"' tests/ companion_harness/ scripts/ docs/ ROADMAP.md README.md` to enumerate every occurrence. Expect ~10–30 lines; v0.1j had a similar count.
2. **Bump the literal first** in `companion_harness/speak_policy.py`. Run `pytest tests/ -q` under canonical venv; expect failures equal to the count of pinning fixtures.
3. **Migrate each pinning fixture** to `"v0.2-final"` in surgical edits — one file at a time; verify the count of failures drops by exactly the count of changed assertions.
4. **Mirror `v0_1h_replay_report.py`** into `v0_2_replay_report.py`. Sections required:
   - Header: milestone, POLICY_VERSION, date.
   - Per-wave report (Waves 1–6 from roadmap-v0.2-draft.md; each wave lists its tasks + status + key gate).
   - Numeric gates verification (every gate from roadmap-v0.2-draft.md "Numeric gates table" gets one line: pass/fail/informational).
   - POLICY_VERSION verification block: `assert POLICY_VERSION == "v0.2-final"` and replay-exact gate result.
   - Milestone-tag readiness: print "READY FOR `git tag v0.2`" if all blocking gates pass.
5. **Smoke run** the new script: `python scripts/v0_2_replay_report.py` under canonical venv; assert it prints the readiness banner.
6. Run the full test suite under canonical venv; assert green.

**Test plan**
- `pytest tests/ -q` under canonical venv passes after migration.
- `python scripts/v0_2_replay_report.py` under canonical venv prints the readiness banner.
- `grep -rn '"v0.1k"' tests/ companion_harness/ scripts/` returns zero matches in policy-version-pinning contexts (historical references in plan/roadmap docs are allowed).

**Success criterion**
```
pytest tests/ -q  # under canonical venv, passes
python scripts/v0_2_replay_report.py  # prints READY FOR git tag v0.2
grep -rn '"v0.1k"' tests/ companion_harness/ scripts/  # zero matches (except historical comments)
```

**Anchors locked** — Anchor 1 (two-step POLICY_VERSION cadence; v0.1k → v0.2-final is the final step).

**OQs (pre-resolved leans)**
- OQ-7a: One PR for bump + sweep + script, or split? **Lean: ONE PR** — same as v0.1j Task 18; the bump is meaningless without the sweep, and the script verifies the bump. Splitting makes intermediate states broken-on-purpose.
- OQ-7b: Include a deprecation grace period for `"v0.1k"` strings? **Lean: NO** — this is a flight-recorder harness; replay fixtures pin a specific version on purpose. Migrate cleanly.
- OQ-7c: Add a `MIGRATION.md` note? **Lean: NO** — `scripts/v0_2_replay_report.py` IS the migration artifact; duplicating it in prose is over-eng.

**Cross-references** — `companion_harness/speak_policy.py:19` (POLICY_VERSION literal); `scripts/v0_1h_replay_report.py` (template); `docs/plan-v0.1j-execution.md` Task 18 (precedent pattern); roadmap-v0.2-draft.md Anchor 1 + Wave 6 Task 20.

**Blocker dependencies** — T1 through T6 merged. v0.1k POLICY_VERSION (from v0.2b) MUST be live on main (without this, the "from" string is wrong).

---

## Task 8 — `git tag v0.2` (operator-only step)

**Files touched** — none in-repo. This is a git operation, not a code change.

**Implementation sketch**
1. After T7 merges to main, the operator runs:
   ```bash
   git checkout main
   git pull --ff-only
   python scripts/v0_2_replay_report.py  # verify READY FOR git tag v0.2
   git tag -a v0.2 -m "v0.2 milestone — production-quality capability stack"
   git push origin v0.2
   ```
2. Optional: create a GitHub release pointing at the tag. Lean: operator's call; not required for the milestone.

**Test plan** — `git tag --list | grep v0.2` returns `v0.2`.

**Success criterion**
```
git tag --list | grep '^v0\.2$'  # returns v0.2
git show v0.2 --stat | head -1   # shows the tag commit
```

**Anchors locked** — none new. Anchor 1 (v0.2-final tag completes the cadence).

**OQs (pre-resolved leans)**
- OQ-8a: Annotated tag or lightweight? **Lean: ANNOTATED** (`-a`) — annotated tags carry author + date + message; lightweight tags don't. The audit trail wants the metadata.
- OQ-8b: Signed tag (`-s`)? **Lean: NO unless operator has GPG set up** — signing requires GPG configuration that may not be on every operator's box; absence-of-signature is not a regression.
- OQ-8c: Tag at `v0.2` or `v0.2.0`? **Lean: `v0.2`** — matches roadmap-v0.2-draft.md naming and Anchor 1 phrasing ("`v0.2-final`" is the POLICY_VERSION string; `v0.2` is the git tag).

**Cross-references** — roadmap-v0.2-draft.md Wave 6 Task 20; T7 (the precondition).

**Blocker dependencies** — T7 merged to main.

---

# §Numeric gates with Measurement column

| Metric | Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|---|
| `replay_report_export_determinism` | == 1.0 (byte-identical across runs, modulo `generated_at_wall`) | T1 owner | Two consecutive `export_replay_report()` calls on the same fixture; compare via `diff -r` after normalizing `generated_at_wall` | All fixture sessions in `tests/fixtures/replay_report_export/` | blocking |
| `replay_report_tar_byte_stability` | == 1.0 (byte-identical tar bytes across runs) | T2 owner | Two consecutive `export_replay_report_tar()` calls; compare `sha256sum` of output tar files (after manifest normalization) | One fixture session | blocking |
| `blob_retention_rotation_correctness` | == 1.0 (only files older than window deleted; newer files preserved) | T3 owner | Fixture `blob_dir` populated with files at controlled mtimes; assert post-rotation directory contents match expectation set | One synthetic blob_dir per test case (≥ 3 cases: 0-day, 7-day, 30-day boundary) | blocking |
| `healthz_per_adapter_readiness_coverage` | == 7 (one bool per adapter: VAD, SmartTurn, Backchannel, ASR, TTS, Vision, ForegroundModel) | T4 owner | `curl /healthz | jq 'keys'` lists 7 `*_ready` keys; type-check each is bool | One `/healthz` response | blocking |
| `healthz_gpu_memory_keys_present` | == 4 (allocated_mb, reserved_mb, total_mb, device_name; all `int|None` or `str|None`) | T5 owner | `curl /healthz | jq 'has("gpu_memory_allocated_mb")'` returns true for all 4 keys | One `/healthz` response | blocking |
| `healthz_event_rate_counter_accuracy` | absolute error < 1 event/sec on synthetic 60s window | T6 owner | Inject N events with controlled `timestamp_mono_ms`; compute expected rate; compare to reported `events_per_second_last_60s` | ≥ 60s of synthetic events (≥ 60 events) | blocking |
| `healthz_response_additive_compatibility` | == 1.0 (every pre-v0.2f key still present + same type) | T4/T5/T6 collective owner | Snapshot pre-v0.2f `/healthz` keys; assert each remains in post-v0.2f response with same type | One snapshot pair | blocking |
| `policy_replay_exact_v0_2_final` | == 1.0 | T7 owner | `test_policy_replay_exact` across all v0.1 + v0.2 fixtures at the v0.2-final POLICY_VERSION pin | All policy-version-pinning fixtures | blocking |
| `policy_version_literal_v0_2_final` | == 1 (exactly one POLICY_VERSION literal, value == "v0.2-final") | T7 owner | `grep -n 'POLICY_VERSION = ' companion_harness/speak_policy.py` returns exactly one line with `"v0.2-final"` | The single source file | blocking |
| `v0_2_replay_report_readiness_banner` | == 1 (`READY FOR git tag v0.2` printed) | T7 owner | `python scripts/v0_2_replay_report.py | grep 'READY FOR git tag v0.2'` returns one match | One script run | blocking |
| `local_ci_pass_rate` | == 1.0 | Each task owner | `pytest` pass rate on the local-only contract test suite per `docs/remote-dev.md` | All contract tests | blocking |
| `remote_smoke_pass_rate` | == 1.0 | Each task owner | `pytest` pass rate on the b200 GPU smoke suite per `docs/remote-dev.md` (for T5 GPU memory smoke specifically) | All b200-tagged smoke tests | blocking |
| `v0_2_milestone_tag_present` | == 1 (`git tag --list` includes `v0.2`) | T8 (operator) | `git tag --list | grep '^v0\.2$'` returns exactly one match | One git query | blocking |

---

# §Dependencies (explicit final-sub-stage statement)

**v0.2f is the LAST v0.2 sub-stage.** All prior v0.2 sub-stages MUST merge before T1 starts:

- **v0.2a** (whatever Wave 1 / BackgroundReasoner sub-stage maps to) — merged.
- **v0.2b** (whatever Wave 2 / diarization sub-stage maps to, INCLUDING the v0.1j → v0.1k POLICY_VERSION bump from Task 9) — merged.
- **v0.2c** (whatever Wave 3 / real-mode benchmark loaders sub-stage maps to) — merged.
- **v0.2d** (whatever Wave 4 / Eval Phase C sub-stage maps to) — merged.
- **v0.2e** (whatever Wave 5 / default-on cascade sub-stage maps to) — merged.

T7's POLICY_VERSION sweep assumes `companion_harness/speak_policy.py:POLICY_VERSION == "v0.1k"` on main; if v0.2b has not landed, T7 is blocked. Coordinator must verify the `"v0.1k"` literal is live before dispatching T7.

T8 (`git tag v0.2`) is operator-only and runs after T7 merges. No PR.

---

# §Discipline

This plan is **pure docs**. It describes capability work that follow-up PRs implement; this PR itself adds only `docs/plan-v0.2f-execution.md`.

Scope is **capability-only** per v0.2 Anchor 5. No Docker, systemd, k8s, secrets, log shipping. Reviewers who see those artifacts in a v0.2f follow-up PR reject and route to v0.3.

Per coding rule 4 (one PR, one outcome): each Task 1–7 ships as its own PR (with the lean exception that T4/T5/T6 may combine into one PR per OQ-4b to avoid rebase tax on `_handle_health`). T8 is a git tag, not a PR.
