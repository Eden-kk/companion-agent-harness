# v0.2 milestone released — 2026-05-16

Tag: `v0.2` · Commit: `f676df0` · Branch: `main`

This document is a sober account of what shipped in the v0.2 milestone, what readiness gates were verified, and what remains open. It is not a celebration.

---

## What v0.2 ships

### v0.2a — Background reasoner foundation (PR #291)

`MCPBackgroundReasoner` adapter wraps the MCP streaming-progress protocol behind the `BackgroundReasoner` interface already present in `companion_harness/`. `ConfigStore` gained Tier-B budget slots (`reasoner.max_tokens_per_turn`, `reasoner.max_wall_ms`). Two new event types land: `signal_producer_fallback` (emitted whenever a signal producer degrades to a cheaper path) and `reasoner_budget_exhausted` (emitted when the MCP call hits a budget ceiling). The smart-path foreground model checks budget exhaustion before committing to a proposal — it reads the ConfigStore budget and emits `reasoner_budget_exhausted` if exceeded rather than silently truncating.

### v0.2b — Pyannote diarization adapter + POLICY_VERSION bump (PR #296)

`PyannoteDiarizationAdapter` wraps pyannote.audio speaker diarization behind the `DiarizationAdapter` interface. `current_speaker_id` now flows into `PolicyInputs` so the speak-policy layer can use speaker continuity in its decision. Concurrent with this, the mute window in the orchestrator is now driven by the `is_synthesizing` flag (previously it used a fixed timer); this eliminates the false-early-unmute that appeared when Kokoro flushed faster than expected. POLICY_VERSION bumped atomically from `v0.1j` to `v0.1k` alongside this change.

### v0.2c — Real CANDOR + FullDuplexBench HF streaming loaders (PR #295)

`CandorCaseSource` and `FullDuplexBenchV1/V1.5CaseSource` grew real streaming implementations backed by Hugging Face `datasets`. Both are gated behind `HF_TOKEN`: if the environment variable is absent, the adapters fall back to synthetic mode and emit a warning rather than crashing. Revision pinning uses `revision=<commit-hash>` on every `load_dataset` call so that eval runs are reproducible. A `--filter K=V` CLI flag was considered but deferred. The eval runner now enforces `dataset_row_skip_rate < 0.05` as a hard gate — if more than 5% of rows are skipped (schema mismatch, decode error), the run aborts rather than producing a misleading metric. `--mode {synthetic,real}` and `--limit N` CLI flags allow quick local smoke tests without hitting the full dataset.

A license-pin guard was added: if the HF dataset card's `license` field changes from the pinned value, the loader raises `DatasetLicenseChangedError` so operators notice before unknowingly ingesting data under a different license.

### v0.2d — Eval Phase C: live examiner + replay-match-rate metric (PR #297)

`LiveExaminerCaseSource` drives the harness with real audio from a live session and examines policy decisions in flight. Phase C fixture pack carries a `schema_version` field and uses the Kokoro TTS stub (not the real ONNX model) for determinism in CI. `ReplayMatchRateVsGroundTruthMetric` computes agreement between a recorded trace and a ground-truth trace using `caused_by` DAG lookup — not a positional zip — so insertions and deletions in the event stream do not corrupt the alignment. The `xfail` marker was removed from `test_eval_run_replay_safe` because the Phase A.5 replayer is now complete.

### v0.2e — 6 adapters flipped to default-on (PR #294)

Six adapters that previously required explicit opt-in flags are now on by default:

| Adapter | Old default | New default | Rollback flag |
|---|---|---|---|
| `CLIPSceneChangeScorer` | OFF | ON | `--no-enable-clip-scene` |
| `HeuristicAVConflictScorer` | OFF | ON | `--no-enable-av-conflict` |
| `MiniCPMDeicticDetector` | OFF | ON | `--no-enable-deictic` |
| `ProsodyLexiconUrgencyScorer` | OFF | ON | `--no-enable-urgency` |
| `GroundingDINOAdapter` | OFF | ON | `--no-enable-grounding` |
| `SentenceTransformerEmbedder` | OFF | ON | `--no-enable-embeddings` |

All six were switched using `argparse.BooleanOptionalAction` so that `--no-enable-X` reverts to the null-stub posture without changing the interface. Stage-2 timing instrumentation was also added: `scene_change_score_ms`, `grounding_confidence_ms`, and `av_conflict_ms` are now recorded on every frame cycle for profiling on b200.

### v0.2f — Deployability pack (PR #298)

`export_replay_report()` produces a tar bundle that is byte-stable across Python versions (deterministic sort order, no mtime). A blob rotation worker runs as an asyncio background task: it scans the blob dir every `--blob-rotation-interval-s` seconds and tombstones blobs older than `--blob-retention-days`. `/healthz` was extended to expose per-adapter readiness, GPU memory (via `nvidia-smi` subprocess when available), and event rate (events/s over the last 10 s window). `/metrics` exposes a Prometheus-compatible text endpoint at the same port. `scripts/v0_2_replay_report.py` prints a readiness banner that operators can use to confirm a deployment is healthy before accepting traffic.

### v0.2 final — POLICY_VERSION bump (PR #299)

POLICY_VERSION was bumped atomically from `v0.1k` to `v0.2-final` in a dedicated commit with no other changes. All 19/19 readiness gates recorded as MET.

---

## Readiness gates (19/19 MET)

The three b200-resident gates verified directly on the machine:

| Gate | Threshold | Measured |
|---|---|---|
| `remote_smoke_pass_rate` | ≥ 19/20 | 20/20 |
| `direct_question_latency_p50` | < 800 ms | verified MET |
| `direct_question_latency_p95` | < 1500 ms | verified MET |

All other gates (contract test pass rate, orphan count = 0, Tier-B replay bit-identical, etc.) run in CI and were green at tag time.

---

## Dashboard P1 wave (this session)

Five PRs added the hot-seam model-swap panel to the Tier-B dashboard:

- **PR #286** — `ConfigStore.seam_state` field + `POST /config/seam` HTTP surface
- **PR #285** — `model_swap_requested`, `model_swap_rejected`, `model_swap_completed` event emission
- **PR #288** — Frontend toggle UI (12 hot-seam rows in the tuning drawer; real ↔ disabled per seam)
- **PR #289** — Contract tests for model_swap event chain
- **PR #290** — Live-pipeline factory honors `ConfigStore.seam_state` (a `disabled` seam gets `None` instead of the real adapter, with no restart required)

---

## Eval-console PR1 (this session)

- **PR #293** — REST polling backend + static asset bundle + 6-adapter dispatcher. Eval runs launched from `/eval.html`; results poll `/eval/status/<run_id>` until terminal state.

---

## Raw-mode TTS fixes (this session)

Three fixes to `--minicpm-streaming-raw` mode landed in this session:

- **PR #284** — Switched dedup strategy from skip-if-busy to cancel-previous (newest proposal wins).
- **PR #287** — Fixed first TTS task producing no audio (root cause: exception swallowed twice in `_tts_worker`).
- **PR #292** — Switched again from cancel-previous to debounce-then-synthesize (300 ms window) to prevent audio gaps on rapid proposals.

---

## Known open items

1. **`--minicpm-streaming-raw` produces no audio in practice.** Bypassing VAD/EOU removed the trigger that tells MiniCPM "now generate speech." The fix is to re-enable VAD as an EOU-only gate while keeping the raw-mode SpeakPolicy bypass. Issue to file.

2. **`speaker_continuity_anchor` event emission deferred.** The schema field and `PolicyInputs` slot landed in v0.2b, but the emitter code was deferred. Diarization flows into policy but the anchor event is never emitted yet.

3. **`--filter K=V` CLI flag deferred from v0.2c.** The eval runner has `--mode` and `--limit`; per-field filtering was scoped out.

4. **`_CANDOR_REVISION` constant has a TBD placeholder.** Operators must fill in the commit hash from the accepted HF dataset version after accepting the dataset license on b200. Without it, `revision=TBD` will cause HF to resolve `main`, which breaks reproducibility.

---

## PR count this session

PRs included in this session (v0.1e → v0.2 tag, numbered):

#243, #252, #253, #254, #255, #256, #257, #258, #259, #260, #261, #262, #263, #264, #265, #266, #267, #268, #269, #270, #271, #272, #273, #274, #275, #284, #285, #286, #287, #288, #289, #290, #291, #292, #293, #294, #295, #296, #297, #298, #299

**Total: 41 PRs merged this session.**
