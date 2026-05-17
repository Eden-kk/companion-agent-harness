# Progress — post-v0.2 (2026-05-16)

Date: 2026-05-16 (later in the day, after the v0.2 release earlier the same day).

v0.2 tag: `f676df0` · post-v0.2 HEAD at time of writing: `e0fc45c` (PR #329).

This document picks up where `docs/progress-v0.2-released.md` left off.
All PRs below are merged to `main` unless noted otherwise.

---

## §1 — Manual-test-driven fix wave (rounds 1–3)

Three rounds of test-assistant sessions against the live console on port 8800 produced
findings that were triaged, filed, and resolved in three PR batches.

### Round-1 fixes (#301–#310)

Findings from the first session (HEAD `f676df0`, v0.2 tag).

- **PR #301** (F1 diagnosability) — `MISSING_SIGNAL_PRODUCER` `ReasonCode` added so
  the silence caused by "no addressing signal" is visible in `policy_decision.payload_inline`
  rather than appearing identical to `NOT_ADDRESSED_TO_AGENT`.
- **PR #302** (N1) — `/metrics` Prometheus endpoint registered in `server.py`; route had
  been doc-claimed but never mounted.
- **PR #303** (F2) — Dashboard event filter UI: type-chip checklist, reason-code dropdown,
  search box, pin; connected to a filter-aware subscriber path that replaces the legacy
  direct-append loop.
- **PR #304** (N2 + N4) — Status banner in `manual-test-handbook.md` corrected: eval URLs
  (`/eval`, `/eval/runs`, `/eval/runs/{id}`), `/healthz` field names (`gpu_memory_allocated_mb`,
  `events_per_second_last_60s`, flat adapter keys), hot-seam endpoint (`POST /config/model-swap`).
- **PR #305** (F0d) — EventLogger ring raised from 4096 → 16384; `DisplayBroker` added
  high-rate frame event sampling (`raw_audio_chunk`, `vad_frame`) before fan-out, cutting
  `log_drop_or_degrade` count from ~78/12 s to ~11–13/12 s on equivalent synthetic load.
- **PR #306** (F0a + F0b + F0c sketch) — Three orchestrator races: (a) `_decision_in_flight`
  reset moved to `finally:` after `play_task` completion so a second TTS cannot start during
  active playback (F0a); `coalesced_during_playback` event emitted when T2 suppresses a turn
  signal. (b) `_tee_to_foreground` drained at batch-open before `process_stream` starts so
  stale inter-turn frames do not prefix T3's input (F0b). (c) `proposal_batch_window_ms`
  kwarg default raised 200 → 600 ms in `build_live_pipeline()` — note: ConfigStore schema
  cap still needed a separate fix (see R2-1, PR #308). Proposal grace extended 200 → 600 ms.
- **PR #307** (N5) — `MiniCPMStreamingModel.chat()` wired to set deictic default-on so
  `/healthz` reports `"deictic_model": "real:MiniCPMDeicticDetector"` as v0.2e claimed.
- **PR #308** (R2-1 + R2-4 + R2-5) — Three follow-up fixes from round-2 review: (a) config
  schema `proposal_batch_window_ms` default raised 80 → 600, max raised 200 → 1500 (R2-1 dead-code
  closure); (b) `schema_version` literal unified to `"0.1"` across all server-emitted events
  (R2-4); (c) `blob_rotation_alive` boolean added to `/healthz` (R2-5).
- **PR #309** (R2-2 + R2-3) — `build_app()` default `display_sampling_rate` reverted 5 → 1
  (test-safe default) after PR #305's sampling broke three contract tests (two `TimeoutError`
  waiting for `raw_audio_chunk` / `vad_frame` events; one orphan-count failure from sampled-out
  VAD frame refs).
- **PR #310** — Audit panel noise reduction: payload preview replaces the raw
  `ts/src/seq/caused_by/hash` metadata line on each row; full metadata surfaces on hover in
  a tooltip. Type-aware preview strings (e.g., `addressing_classified` → `addressed=true
  (MiniCPMAddressingClassifierImpl)`).

### Round-2 fixes (#308 + #309)

Round-2 tested HEAD `a4c91e4` (after the 7 round-1 PRs). Three new findings:

- **R2-1** (BLOCKER) — `proposal_batch_window_ms` schema default/max not bumped alongside the
  kwarg; ConfigStore overwrite at every EOU made PR #306's 600 ms grace a dead path. Fixed in
  PR #308.
- **R2-2** (BLOCKER) — `scripts/v0_2_replay_report.py` reported NOT READY (3 contract test
  failures) caused by PR #305's sampling. Fixed in PR #309 (revert display sampling default).
- **R2-3** (CONCERN) — DisplayBroker sampling created a new variant of the G1 phantom-ref
  issue (VAD-frame IDs in `caused_by` of downstream events, but sampled out of `/ws/display`).
  Fixed alongside R2-2 in PR #309.
- **R2-4** (CONCERN) — schema_version inconsistency. Fixed in PR #308.
- **R2-5** (NIT) — `blob_rotation_alive` absent from `/healthz`. Fixed in PR #308.

### Round-3 fixes (#312–#317)

Round-3 tested HEAD after the round-2 batch. New findings:

- **PR #312** (N6) — `/eval/runs/{id}/event_logs/{case_id}` always returned 404 because
  the reader looked for `<run_id>/<case_id>/events.jsonl` while the writer produced
  `<run_id>/event_logs/<case_id>.jsonl`. Reader path rewritten to match writer.
- **PR #313** (F3) — Dashboard filter did not actually filter: legacy `renderEvent`
  direct-append ran after the filter-aware subscriber's `applyFilter` clear-and-rebuild, re-
  inserting every event regardless of checkbox state. Legacy append deleted.
- **PR #314** (N7) — Eval manifest serialized Python `None` as the literal string `"None"`;
  changed to JSON `null` for `event_log_path` on synthetic-only cases.
- **PR #315** (F4) — Typed `payload_inline` added to `signal_producer_fallback` and
  `synthesis_skipped_no_proposal` (previously `payload_inline: null`). Shapes:
  `{producer, from_path, to_path, reason}` and `{dispatcher_state, batch_window_ms,
  batch_open_at_ms, batch_close_at_ms, signal_evt_id}`.
- **PR #316** (F1b) — `MiniCPMAddressingClassifier` wired: `server.py` now passes the loaded
  foreground model into `build_live_pipeline(minicpm_text_model=...)`. Previously `_NullMiniCPM`
  always won; every `addressing_classified` event used `WakeWordAddressingClassifier` as the
  primary, producing 83% `NOT_ADDRESSED_TO_AGENT` silence on natural conversation.
- **PR #317** (G1) — `MiniCPMStreamingModel.set_session` wires the session logger so
  `native_duplex_invocation` events are actually logged (root of the `-nd-<seq>` phantom-ref
  pattern on the MiniCPM path).

### Polish + survey (#318–#323, #326)

- **PR #318** (F2 cap) — Audit panel retained-line cap: default 500 rows, operator-controlled
  via a selector; prevents unbounded DOM growth in long sessions.
- **PR #319** — Basic-stack design doc: `docs/basic-stack-design-2026-05-16.md` written as a
  self-contained reference for the post-fix MiniCPM + VAD + ASR + TTS stack. Documents the
  end-to-end audio path, per-hop latency table, invariant contracts, and quality-assurance plan.
- **PR #320** — Wake-word-on-audio issue filed (F1 voice-mode addressing design, tracked for
  v0.3); issue #320 links the F1 finding to the planned fix.
- **PR #321** (R2-3 closure) — DisplayBroker rework: per-subscriber queue raised 256 → 1024;
  `display_subscriber_drop` event emitted with `{subscriber_id, subscriber_drop_count,
  dropped_event_type, queue_depth}` payload when a subscriber queue saturates; default-OFF
  in the dashboard type filter (high-volume; operator enables to diagnose drop storms).
- **PR #322** — Logprob addressing classifier: binary classification via token log-probabilities
  replaces the fragile chat-parse path as the root fix for addressing quality. Emits
  `addressing_classifier_low_confidence` when `confidence ∈ [0.45, 0.55]`.
- **PR #323** — F0a auto-wait-for-tts driver verify: `repro_f0a_f0b.py` `auto` mode adds a
  wait-for-TTS step to confirm F0a fix holds end-to-end under driver load.
- **PR #326** — 14-component streaming-speculative survey: `docs/streaming-speculative-
  component-survey.md` written as a reference for v0.3+ planners, covering every audio-path
  component's current state, streaming opportunity, speculative opportunity, and SOTA references.

---

## §2 — Path B (streaming-speculative + integrated barge-in) in flight

Plan: `docs/plan-streaming-speculative-bargein-execution.md`
(branch `docs-plan-streaming-speculative-bargein`; plan-critic converged READY 2026-05-16).

Target milestone: v0.3. Default flag OFF — no behavior change until cut-over PR 8.

Five open questions resolved 2026-05-16:

1. **Flag default** — OFF. Plain boot is unchanged Path A behavior.
2. **Flag type** — HARD CLI + config (not a runtime toggle).
3. **EOU signal under Path B** — MiniCPM `_last_is_listen` is the primary EOU; VAD stays as
   safety-net and barge-in onset producer.
4. **`POLICY_VERSION` bump** — not required by Path B alone (no policy-layer semantics change;
   the ring commit/discard is an orchestrator concern).
5. **Latency target** — 95% of turns with first-TTS-chunk within 300 ms EOU→TTS under Path B.

Pre-§3.5 gate cleared 2026-05-16: `MiniCPMODuplex.reset_session(reset_token2wav_cache: bool)`
is the verified reset API; `reset_audio_past_key_values()` does not exist. §3.5 pivots from
suppress to pre-empt: call `reset_session(reset_token2wav_cache=False)` at `_last_is_listen=True`
listen boundaries, before the internal auto-reset fires.

### Sub-plan status

| Sub-plan | PR | Status |
|---|---|---|
| 3.1 Kokoro sub-chunking (≤200 ms PCM slices) | #324 | MERGED |
| 3.2 `_dropped_before_enqueue` root cause + tee depth 64 → 256 | #325 | MERGED |
| 3.7 Feature flag skeleton (default OFF, no behavior change) | #327 | MERGED |
| 3.3 Path B core (continuous proposer + commit/discard) | #328 | UNDER REVIEW |
| 3.4 Integrated barge-in | — | PENDING (after 3.3) |
| 3.5 KV cache windowed reset | — | PENDING (after 3.3) |
| 3.6 Audit story for discarded speculations | — | PENDING (after 3.3) |
| PR 8 flag-day cut-over | — | PENDING (after 3.4 + 3.5 + latency baseline) |

PR #328 is the first PR under the flag that changes observable behavior; all prior sub-plans
are either prerequisites or the flag stub itself.

---

## §3 — Quality-of-life polish (since v0.2 tag)

- **Pinned basic-stack defaults at fresh startup (PR #329)** — `GET /config/seams` on a
  freshly-booted server now returns vad/asr/tts enabled, all 9 other hot seams disabled.
  No operator action required to reach the tested basic-stack configuration.
- **Audit panel line cap (PR #318)** — default 500 rows; prevents multi-hour session DOM growth.
- **/metrics endpoint registered (PR #302)** — Prometheus scrape confirmed working
  (`harness_uptime_seconds`, `harness_adapter_ready{adapter="vad"} 1`, etc.).
- **`blob_rotation_alive` in /healthz (PR #308)** — blob rotation worker liveness now
  surface-visible without log scraping.
- **Audit row payload preview + hover tooltip (PR #310)** — event-type-aware preview replaces
  raw metadata line; full metadata (ts/src/seq/caused_by/hash) on hover.

---

## §4 — Known open items

1. **Path B sub-plans 3.4–3.6 + PR 8 cut-over** — see §2 status table.
2. **Latency baseline capture (Block 3)** — needs a ~10-minute operator window during which
   the console is taken offline for the scripted latency sweep. Operator-scheduled; not yet run.
3. **F1 voice-mode addressing design (issue #320)** — wake-word-on-audio path or
   `address=unknown → allow` fallback so the basic stack stays functional when ASR is toggled
   off. Tracked for v0.3 alongside Path B.
4. **`--minicpm-streaming-raw` produces no audio** — pre-v0.2 known limitation; deferred.
   Bypassing VAD/EOU removed the generation trigger. Not in scope for any current sprint.

---

## §5 — PR count this session (post-v0.2)

PRs merged since the v0.2 tag (`f676df0`) through HEAD `e0fc45c`:

#301, #302, #303, #304, #305, #306, #307, #308, #309, #310, #311, #312, #313, #314, #315,
#316, #317, #318, #319, #320, #321, #322, #323, #324, #325, #326, #327, #329

**Total: 28 PRs merged post-v0.2 this session.**

(PR #328 is under review at time of writing and not counted.)
