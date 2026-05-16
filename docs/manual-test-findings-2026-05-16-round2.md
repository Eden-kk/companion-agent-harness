# Round-2 manual test findings — 2026-05-16

Target: `v0.2` + 7 fix PRs (#301–#307) merged. Console restarted at HEAD `a4c91e4`.

Session role: test-assistant (observation only, no production code modified beyond live ConfigStore patches via the public `POST /config/patch` API).

Probes used: `curl` against the running console at `http://localhost:8800`, `tests/manual/repro_f0a_f0b.py auto`, `tests/manual/playwright_dashboard_smoke.py`, ad-hoc scripts under `/tmp/module_*.py`, and `scripts/v0_2_replay_report.py`.

---

## Fix verification (Round-1 findings)

| Finding | Fix PR | Verified? | Evidence |
|---|---|---|---|
| F0a (duplicate TTS during playback) | #306 | **PARTIAL** | Code paths confirmed: `realtime_orchestrator.py:546-557` emits `coalesced_during_playback` when `_decision_in_flight` AND `_audio_output.is_playing`; the `False`-reset is in the `finally:` block after `play_task`. No live overlap reproduced in the round-2 captures because Module E's auto sweep at 800 ms inter-utterance produced ONLY ONE proposal+TTS (the second turn died on F0c, not stacking), so the F0a race window never opened. Need a harder forcing function to actually trip the race in steady-state, but the patch is in place. |
| F0b (stale-frame leakage) | #306 | **YES (code)** | `realtime_orchestrator.py:958-965` drains `_tee_to_foreground` after `_batch_open_event.clear()` before `process_stream` starts. No drain-event is emitted (silent fix). Code patch matches the round-1 minimum-viable-fix recipe. |
| F0c (`synthesis_skipped_no_proposal` on 2nd turn) | #306 | **NO** | See **R2-1 below**: PR bumped the `build_live_pipeline()` kwarg default 200→600 ms, but **the ConfigStore schema for `orchestrator.proposal_batch_window_ms` still has `default=80, max=200`**. `_snapshot_config()` (orchestrator.py:1214) overwrites the kwarg every EOU. Live `/config` shows value=80. Reproduced: at 800 ms inter-utterance with the live config, the 2nd turn still emits `synthesis_skipped_no_proposal`. Even after `POST /config/patch` to the schema-max 200, the 2nd turn still skips. PR #306's claimed fix is dead code unless the ConfigStore default/max are bumped too. |
| F0d (`log_drop_or_degrade` rate) | #305 | **YES** | Drop count fell from round-1's 78 (12 s, 800 ms gap) → 11–13 in equivalent round-2 captures. Sampling + larger ring is working. Caveat: high-rate forcing (2 utterances at 200 ms gap) still produces 40 drops — better than 78 but not zero. |
| F1 (silent-fail when ASR disabled) | #301 | **CODE LANDED (not driver-verified)** | PR #301 is diagnosability-only — adds `MISSING_SIGNAL_PRODUCER` ReasonCode. The round-1 design defect (ASR-required-for-speech path) was explicitly deferred. I did not toggle ASR-off this round to avoid leaving the operator UI in a degraded state; the new ReasonCode in `speak_policy.py` is present per the merged diff. |
| F2 (dashboard panel noise) | #303 | **YES** | Playwright body preview shows `search:`, `reason code: all`, `Showing 0 of 0`, `types:` chip row — filter UI present in the dashboard panel. |
| N1 (`/metrics` endpoint missing) | #302 | **YES** | `GET /metrics` returns HTTP 200, `Content-Type: text/plain; version=0.0.4; charset=utf-8`, body includes `harness_uptime_seconds`, `harness_events_per_second_last_60s`, `harness_adapter_ready{adapter=...}`, `harness_gpu_memory_allocated_mb{device=cuda:0}`. |
| N2 (handbook eval URLs wrong) | #304 | **DOC-ONLY (cannot verify here)** | PR #304 was a docs banner update, not new API aliases. The 404s persist for `/eval.html`, `/eval/run`, `/eval/status/<id>` because those routes were never added; the handbook now matches the actual routes `/eval`, `/eval/runs`, `/eval/runs/<id>`. Verified `/eval` 200, `/eval/runs` 200, the legacy paths still 404. |
| N4 (`/healthz` field shape divergence) | #304 | **YES (matches code; doc fixed)** | `/healthz` returns `gpu_memory_allocated_mb`, `gpu_memory_reserved_mb`, `gpu_memory_total_mb`, `events_per_second_last_60s`, flat adapter keys. PR #304's banner gates were always written against the actual code; the handbook is now aligned. |
| N5 (Deictic stub on default-on) | #307 | **YES** | `/healthz` now shows `"deictic_model": "real:MiniCPMDeicticDetector"`. Live capture also contains `deictic_classification` events with non-stub `source`. |

**Headline:** 7/10 round-1 findings effectively fixed. **F0c is the standout regression — the PR title claims 200→600 ms grace bump but the operative bump is in unused code.** F0a/F0b code-fixed but not driver-verified end-to-end (would need a longer playback + faster second turn to force the race).

Round-1 findings **NOT in scope for this batch** (still broken — confirmed reproducible):

- **G1** — null-detector `-nd-` dangling `caused_by` refs still present (6 instances in round-2 Module G capture, plus newly-observed VAD-frame phantom refs — see **R2-3** below).
- **N6** — `/eval/runs/{id}/event_logs/{case_id}` still 404 (writer/reader path mismatch unfixed).
- **N7** — synthetic-only eval cases still serialize `event_log_path: "None"` string instead of JSON `null`.
- **N3** — `/config/seam` (handbook) still 404; actual is `/config/model-swap`.
- **N8** — same root cause as F0c.

---

## New findings (Round 2)

### R2-1 — **BLOCKER** — F0c "fix" (PR #306) is dead code: ConfigStore overrides the 600 ms grace at every EOU

**Evidence:**

- `companion_harness/realtime_orchestrator.py:1214` — `_snapshot_config()` reads `orchestrator.proposal_batch_window_ms` from ConfigStore and overwrites `self._proposal_batch_window_ms` on every iteration of the policy loop.
- `manual_test_console/config_schema.py:136-145` — schema entry still `default=80, max=200, step=10`.
- `manual_test_console/live_pipeline.py:490` — kwarg default raised to 600 (PR #306) but never reaches the running orchestrator.
- Live probe: `GET /config` → `"orchestrator.proposal_batch_window_ms": 80`; schema `"max": 200`.
- Reproduced: `tests/manual/repro_f0a_f0b.py auto --inter-utterance-ms 800` at the live default → `foreground_proposal=1, synthesis_skipped_no_proposal=1` (i.e. 2nd turn silent for the same reason as round-1 F0c). After `POST /config/patch` raising to schema-max 200 ms → still `synthesis_skipped_no_proposal=1` on the 800 ms case. The schema cap prevents reaching the 600 ms value PR #306 actually intended.

**Minimum-viable fix:** raise schema `default` and `max` for `orchestrator.proposal_batch_window_ms` to 600 (or higher) at `manual_test_console/config_schema.py:139-141`. The kwarg change is moot without this.

**Why BLOCKER:** the round-1 finding F0c was explicitly listed in the prompt's "fix verification" goals; the verification fails. The 2nd-turn skip is the regression PR #306 was titled to close.

---

### R2-2 — **BLOCKER** — `scripts/v0_2_replay_report.py` now reports **NOT READY for `git tag v0.2`** (3 contract test regressions)

**Evidence:**

```
Gates total:        19
MET:                18
NOT_MEASURED:       0
FAIL:               1

pytest suite: 1377 passed, 1 skipped, 3 failed

[FAIL] local_ci_pass_rate — threshold == 1.0 — measured "3 failed"

NOT ready for git tag v0.2.
```

Failing tests (in canonical venv `/raid/yid042/venvs/companion-harness`):

1. `tests/test_manual_test_console.py::test_audio_ingest_to_display_roundtrip` — `TimeoutError` waiting for `raw_audio` events on display WS within 5 s.
2. `tests/test_manual_test_live_pipeline.py::test_live_pipeline_emits_vad_and_policy_events` — `AssertionError: orphans: 13 / orphan_count == 0`. Dangling refs are VAD frame ids (e.g. `9c23ae26-...-vad-18-...`) referenced by 7 downstream events (sro, deictic, fm) but never logged. **Same architectural pattern as G1 but extended into the VAD path.**
3. `tests/test_manual_test_live_pipeline.py::test_capture_only_mode_skips_live_pipeline` — `TimeoutError` waiting for `raw_audio_chunk` events.

The two timeouts smell like the F0d display-broker sampling change (PR #305) suppressing `raw_audio_chunk` / `vad_frame` for the display WS path that these tests subscribe to — they sample below the threshold count and time out. The orphan failure is a true causal-graph regression — VAD frame N is referenced as `caused_by` by ≥7 downstream events but the VAD frame itself is no longer logged (likely also a side effect of the sampling change).

**POLICY_VERSION reads `v0.2-final`** ✓ (matches expectation).
**Gates_total = 19** ✓.
**Banner does NOT say "READY FOR git tag v0.2"** — it says the inverse.

**Severity rationale:** the prompt asked specifically that this banner read READY. It does not. v0.2 cannot be re-tagged from this HEAD without either fixing the 3 tests or accepting a banner regression in the headline release artifact.

**Minimum-viable fix:** investigate whether PR #305's sampling logic now drops the FIRST few `raw_audio_chunk`/`vad_frame` events (tests wait for any-N, sampling might delay first-N). For the orphan failure, ensure sampled-out VAD frame ids are not used as `caused_by` predecessors downstream (or log a tombstone for each sampled frame so the chain closes).

---

### R2-3 — **CONCERN** — DisplayBroker sampling (PR #305) creates a new flavor of G1 (VAD-frame phantom refs)

**Evidence:** Module G analysis on `/tmp/repro_round2_800_w200/events.jsonl` (101 events): 78 events reference at least one `caused_by` id that is never emitted to `/ws/display`. The original `-nd-` pattern from G1 contributes 6; the remaining 72 are downstream events (sro, deictic, fm, asr_transcript_emitted, policy_decision) referencing predecessor ids that were sampled out of the display fan-out.

PR #305 added high-rate event sampling at the DisplayBroker level. Functionally correct (the operator UI is less noisy and `log_drop_or_degrade` count fell), but it **breaks DAG closure observable on `/ws/display`** for any consumer that walks `caused_by`. Round-1 reported 20.5% unresolved-ref rate; round-2 measures 77.2% on the same probe.

**Why CONCERN not BLOCKER:** the underlying audit sink (if real, not `_null_sink`) presumably still receives all events — the gap is only on the display fan-out. The dashboard's causal-chain UI is the load-bearing consumer; it now traces broken chains. The pytest test in R2-2 #2 above shows the same issue at a deeper layer.

**Fix direction:** either (a) DisplayBroker must emit a small "sampled out" sentinel per dropped event-id so the chain closes, or (b) downstream consumers receive a synthetic `caused_by` rewrite that collapses sampled refs to the most-recent surviving ancestor.

---

### R2-4 — **CONCERN** — schema_version inconsistency on `/ws/display`

**Evidence:** in the same Module C capture:

- `operator_action`, `model_swap_requested`, `model_swap_completed` events carry `"schema_version": "v0.1f"` (server-emitted, from `manual_test_console.server`).
- `asr_transcript_emitted`, `foreground_proposal`, etc. carry `"schema_version": "0.1"` (orchestrator-emitted).

Two producers writing two literals for the same conceptual field. A replayer that switches on `schema_version` will fork on the producer. Trivial to fix; surprising it survived this long.

**Fix:** pick one literal (probably `"0.1"` per the round-1 positive-verification table that recorded uniform `"0.1"` last week) and update the operator-action emission path.

---

### R2-5 — **NIT** — `/healthz` lacks `blob_rotation_alive` field promised by Module H

The prompt's Module H spec asks for `blob_rotation_alive`-style healthz field. Live `/healthz` has no rotation-related key. The console log confirms the worker started (`Blob rotation: retaining 30 days; tick every 3600s.`) but liveness is not surfaced. Not BLOCKER because Module H still passes via the determinism check, but operator monitoring needs the field.

**Fix:** add `blob_rotation_alive: bool` and `blob_rotation_last_tick_wall: str | None` to `/healthz` payload (server.py near the existing flat-keys block).

---

## Positive verifications (round 2)

- `/metrics` Prometheus endpoint working (N1 closed).
- `/healthz` deictic field reads `real:MiniCPMDeicticDetector` (N5 closed).
- F2 dashboard filter UI present (search box, reason-code dropdown, type chip row, "Showing N of M" counter).
- Module B WS surface — 5 malformed envelope classes survived without crash; concurrent ingest gives distinct session_ids (`sessions_opened` delta=2 for 2 sockets, `active_sessions` returned to 0 on close); late `/ws/display` subscriber gets no backfill (0 events in 300 ms quiet window).
- Module C seam swap — operator_action → model_swap_requested → model_swap_completed chain present on `/ws/display` for safe seam round-trip; invalid seam returns 403 with `model_swap_rejected` audit event.
- Module D harness_native eval run completes 4 cases in <2 s; manifest schema OK; `event_log_path` is a real filesystem string (`/tmp/.../<run_id>/event_logs/<case_id>.jsonl`); files exist on disk.
- Module H `export_replay_report()` byte-identical across two calls (manifest + events both match); `export_replay_report_tar()` byte-identical (10 240 bytes, sha256 match).
- F0d drop count down from 78 → 11–13 on equivalent loads. Sampling worked.
- Module G ReasonCode coverage = 100% on policy_decision events. Required-field coverage = 100% across all 101 events. No long-string payload_inline leakage.

---

## Modules executed

- **A** — DONE. /healthz, /metrics, /config, /config/seams, /config/patch (unknown-key 403 + valid 200), /config/reset, /eval/runs, /eval/adapters all probed. /eval/runs/{id}/event_logs/{case_id} 404 (N6 unfixed).
- **B** — DONE (new vs round 1). 2 concurrent ingest, 5+1 malformed payload classes, late display subscriber backfill check.
- **C** — DONE. Lightweight 1-seam round-trip (attachment_risk_monitor false→true→false), invalid seam, event-chain verification on `/ws/display`. Did NOT toggle all 12 seams (would mutate state too much for a running operator console).
- **D** — DONE. Harness_native synthetic run launched + polled to completion. Voicebench synthetic also probed to confirm N7 (still broken).
- **E** — DONE. 800 ms gap auto-repro at live default (window=80) and at patched 200; 200 ms gap forcing-function. F0a code-confirmed but not race-tripped, F0b code-confirmed (drain in place), F0c regression confirmed (R2-1), F0d improved.
- **F** — DONE. Playwright smoke against `/` and `/eval`. Body preview confirms filter UI on the dashboard.
- **G** — DONE (new vs round 1). 78/101 events have unresolved `caused_by` refs (R2-3). ReasonCode coverage clean. Free-text leakage clean.
- **H** — DONE (new vs round 1). `v0_2_replay_report.py` run (R2-2: banner says NOT READY due to 3 contract test failures). Export determinism + tar determinism both confirmed byte-identical.

---

## Summary

- **Fixes verified: 7/10** (F0b, F0d, F1 code-landed, F2, N1, N4, N5).
- **Fixes regressed/broken/dead-code: 3/10** (F0a partial — code in place but not race-tripped; **F0c dead-code R2-1**; G1 unfixed and **extended** to VAD path R2-3).
- **New findings: 5** (R2-1 BLOCKER, R2-2 BLOCKER, R2-3 CONCERN, R2-4 CONCERN, R2-5 NIT).
- **Net release readiness:** the `scripts/v0_2_replay_report.py` banner now says NOT READY (was READY before the 7 fix PRs). The fix wave fixed visible operator-UI items but broke the contract-test gate and introduced a dead-code F0c "fix". Both R2-1 and R2-2 should block a re-tag attempt.
