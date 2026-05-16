# Manual test plan — v0.2 (`f676df0`) — 2026-05-16

Target: live `manual_test_console.server --port 8800 --enable-vision`, PID 2623442, against tag `v0.2`.

Author: test-assistant session. Driven by APIs + WS + headless Chromium (no human mic/ear). All findings land in `docs/manual-test-findings-2026-05-16.md`.

## Scope and non-goals

**In scope:** every operator-visible surface introduced or modified by v0.2 (see `docs/progress-v0.2-released.md` on branch `docs-v0.2-progress-and-handbook`) plus the core live-loop pipeline.

**Out of scope:**
- The 4 known limitations in handbook §10 (raw-mode no-audio, missing `speaker_continuity_anchor` emitter, no `--filter K=V`, `_CANDOR_REVISION=TBD`).
- Anything that requires a real microphone or speaker (no audio I/O on this headless box).
- Anything that requires `HF_TOKEN` or a license-accepted HF dataset (CANDOR/FullDuplexBench real mode).
- Production-code modification (test-assistant role).

## Methodology

For each module:
1. State the expected behavior (cite handbook / release notes section).
2. Drive the surface with the smallest probe that reaches it.
3. Compare observed against expected.
4. If observed ≠ expected: capture evidence (event excerpt, HTTP response, screenshot), classify severity (BLOCKER / CONCERN / NIT), file in findings doc.

Severity rubric:
- **BLOCKER** — a v0.2 release claim is false in observable behavior, or a CLAUDE.md invariant is violated under reachable conditions.
- **CONCERN** — works but with a sizing/threshold/UX issue that will bite under real load.
- **NIT** — polish.

## Modules

### A — HTTP API surface

Endpoints:
- `GET /healthz` — verify schema vs handbook §9.1 (already partially probed — see N4); add field-by-field check.
- `GET /metrics` — verify Prometheus text format (already 404 — see N1; verify on v0.2 and confirm no other mount).
- `GET /config` — current Tier-B config.
- `POST /config/patch` — patch a single key, verify echoed.
- `POST /config/reset` — reset to defaults, verify state matches `/config` defaults.
- `GET /config/seams` — list seam state (already shape-confirmed).
- `POST /config/model-swap` — request a swap, verify state updates AND `model_swap_*` events emit (cross-checked in Module C).
- `GET /eval/runs` — list runs.
- `GET /eval/runs/{id}` — single-run manifest.
- `GET /eval/runs/{id}/event_logs/{case_id}` — per-case event log.

Out of scope here: WS endpoints (Module B), launching eval runs (Module D), seam-swap event chain (Module C).

### B — WS surface

- `/ws/ingest` — envelope schema (event_type ∈ {raw_audio, raw_video}, base64 payload). Malformed envelope: should be silently dropped per `_handle_ingest_ws`. Verify no crash.
- `/ws/display` — late-subscriber gets fresh events only (no backfill). Verify count & shape.
- `/ws/audio_out` — fan-out across multiple listeners; one tab per session in practice.

Concurrent-session test: open 2 ingest WS, verify both get distinct `session_id`, both visible in `/healthz` `sessions_opened`.

### C — Hot seam swaps + model_swap_* event chain

For each of the 12 seams currently in `GET /config/seams`:
1. Read current state.
2. Toggle to the opposite via `POST /config/model-swap`.
3. Capture `/ws/display` events during the swap.
4. Verify the event chain: `model_swap_requested` → `model_swap_completed` (or `_rejected` if invalid).
5. Re-read `GET /config/seams`, verify the toggle took effect.
6. Toggle back, verify symmetry.

Out of scope: testing that the disabled seam genuinely produces None at the next event cycle (deeper than what /ws/display alone can prove without a fresh ingest).

### D — Eval console

1. `GET /eval/runs` to see what's adapters-available + already-run.
2. Pick the cheapest synthetic adapter (likely `harness_native` or `voicebench synthetic-v1`) per Playwright smoke output.
3. `POST /eval/runs` with synthetic mode + `--limit 1`.
4. Poll `GET /eval/runs/{id}` until status is terminal.
5. `GET /eval/runs/{id}/event_logs/{case_id}` for the first case; verify the events.jsonl is well-formed.
6. Verify the result manifest schema (carries POLICY_VERSION, adapter id, metric values).

### E — Live pipeline behavior under varied timing

Vary `--inter-utterance-ms` of `repro_f0a_f0b.py` (need to extend script to accept this flag — minor edit).

Sweep: 100, 200, 400, 600, 800, 1200 ms.

Metrics per run:
- F0a indicator: count of overlapping TTS runs on /ws/audio_out
- F0c indicator: count of `synthesis_skipped_no_proposal`
- F0d indicator: count of `log_drop_or_degrade`
- F0b indicator: proposal count vs transcript count

Also run a high-volume probe: 5 utterances back-to-back, 200 ms apart, to look for backpressure cliff.

### F — Dashboard UI via Playwright

1. Extend smoke to extract content of "TURNSIGNALS / SPEAKDECISIONS / CAUSAL CHAIN" panel by text-match (since selector probe failed).
2. While an auto repro is in flight, capture panel scroll-buffer length and event line count per second.
3. Compute F2 noise/signal ratio: signal = {policy_decision, model_swap_*, speak_decision, RubricViolation}; noise = everything else.
4. Verify dashboard reflects /config/seams state after a model-swap.
5. Confirm Hot seams toggle UI has the documented 12 rows.

### G — Audit log integrity

Run `repro_f0a_f0b.py auto` for a single 2-utterance pass.

Checks on `events.jsonl`:
- **Invariant 1 (no unlogged behavior):** every event has `event_id`, `timestamp_mono_ms`, `source`, `caused_by` field present (may be empty for roots).
- **DAG closure:** every non-empty `caused_by[]` entry must reference a known `event_id` in this session OR a previously-emitted event id outside the capture window (we can only assert intra-capture).
- **Orphan rate:** count events with `caused_by` references that don't resolve in the capture.
- **ReasonCode coverage:** every `policy_decision` carries `payload_inline.primary_reason_code` (already saw `EOU_CONFIRMED` once).
- **Invariant 6 (free-text):** no event has `payload_inline` containing a sensitive string outside `policy_decision`'s small typed shape.

### H — Deployability scripts

- `python scripts/v0_2_replay_report.py` — verify readiness banner shape; verify it reports POLICY_VERSION=v0.2-final; verify the 19/19 gate count.
- `export_replay_report()` — call twice on the same session, hash both tar bundles, verify byte-identical (deterministic-export claim from PR #298).
- Blob rotation — check `/healthz` if it surfaces blob worker state; otherwise observe `/tmp/manual_test_blobs/` for newly-tombstoned files (won't actually trigger at our retention window; just verify the worker is alive).

## Execution order

A → B → C → D → E → F → G → H, with findings appended to `docs/manual-test-findings-2026-05-16.md` per module.

## Stop conditions

- BLOCKER finding that masks downstream module behavior → halt and flag.
- Server process disappears or `/healthz` stops responding → halt.
- Any modification of production code (would violate test-assistant role) → halt.

## Risk acknowledgment

This plan exercises live ConfigStore mutations (model-swap). Toggling seams during the test session changes the running adapter inventory. State will be left non-default unless explicitly reverted in Module C step 6.
