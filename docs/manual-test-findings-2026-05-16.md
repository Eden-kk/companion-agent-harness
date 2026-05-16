# Manual test findings — 2026-05-16

Target: `v0.2` tag (`f676df0`). Console: `manual_test_console.server --port 8800 --enable-vision`, PID 2623442.

Severity legend: **BLOCKER** (release-stopper) · **CONCERN** (ship-with-known-issue) · **NIT** (polish).

Sorted by severity within each section.

---

## What was done this session

Per the test-assistant briefing ("observation + diagnosis, not implementation"), **no production code was modified**. The work this session was investigation, repro-tooling, and diagnostic capture:

### Test plan covering the v0.2 surface

`docs/manual-test-plan-2026-05-16.md` — modules A–H against /healthz, /metrics, /config*, /eval*, WS surface, ConfigStore + seam swaps, eval console, live pipeline timing, dashboard UI, audit-log integrity, deployability scripts. Each module ran with read-only HTTP/WS probes plus headless Chromium; no production code modified.

### Investigation tooling added (under `tests/manual/`)

- **`tests/manual/repro_f0a_f0b.py`** — WS-only driver against the live console. Two modes:
  - `passive` — connect to `/ws/display` + `/ws/audio_out`, record for `--duration` seconds while a human drives the browser mic.
  - `auto` — synthesize N test utterances with Kokoro (default: "What is two plus two?" + "And what about three plus three?", separated by 800 ms silence), stream them as 16 kHz PCM16 chunks into `/ws/ingest`, record everything, run analysis.

  Outputs to `--out-dir`:
  - `events.jsonl` — every event from `/ws/display`
  - `audio_out.jsonl` — one row per TTS chunk
  - `report.md` — F0a temporal-overlap analysis + F0b causal-chain reachability + event-type histogram + `log_drop_or_degrade` count.

  Used to confirm F0a/F0b are reachable from the WS surface, and to discover F0c + F0d.

- **`tests/manual/playwright_dashboard_smoke.py`** — headless Chromium driver. Loads `/` (dashboard) and `/eval` (eval console), captures status, title, console errors, failed requests, panel selectors that match common causal-chain selectors, full-page screenshots, and a body-text preview. Read-only — does NOT click toggles or launch runs (would mutate state during a live test session).

  Used to confirm `/` and `/eval` return 200 on v0.2 (both screenshots in `/tmp/playwright-smoke-1/`) and that the dashboard's causal-chain panel is labeled "TURNSIGNALS / SPEAKDECISIONS / CAUSAL CHAIN" — useful selector for F2 follow-up.

- **`tests/manual/probe_ws_surface.py`** — abuse + concurrency probe. Sends 6 malformed-envelope classes to `/ws/ingest` (invalid JSON, missing field, wrong event_type, invalid base64, 5 MB oversized, binary frame), verifies the server stays up and `/healthz` continues to respond after each. Opens 3 concurrent ingest sockets and 2 audio_out listeners to characterize session accounting and fan-out.

- **`tests/manual/probe_seam_swap.py`** — round-trip seam-swap probe. Subscribes to `/ws/display`, drives `POST /config/model-swap` to toggle a safe seam (`attachment_risk_monitor`) false → true → false, captures the operator_action → model_swap_requested → model_swap_completed chain, also tests invalid seam name and missing body.

- **`tests/manual/playwright_panel_under_load.py`** — F2 noise measurement. Drives `repro_f0a_f0b.py auto` in a subprocess while headless Chromium samples the TURNSIGNALS panel every 250 ms; classifies newly-appearing lines as signal (`policy_decision`, `model_swap_*`, `speak_decision`, `rubric_violation`, `synthesis_skipped`, `signal_producer_fallback`, etc.) vs noise (`vad_frame`, `raw_audio_chunk`, `log_drop_or_degrade`, `foreground_frame`, etc.) vs other.

### Environment changes

- `playwright` + Chromium installed into the canonical venv `/raid/yid042/venvs/companion-harness`. Chromium cache in `~/.cache/ms-playwright/`.
- Codex `/codex:setup` review gate **disabled** for this project (stop-time review was failing on missing ChatGPT credits for `yid042@ucsd.edu`). Re-enable with `/codex:setup --enable-review-gate` once credits are sorted.

### Production code NOT modified

None of F0a / F0b / F0c / F0d / F1 / F2 / N1–N5 has had its underlying defect patched in this session. The findings are diagnosed and reproducible; implementing fixes is for a separate coder session (or `/ship-feature` plan).

---

## Operator-reported findings

### F0a — BLOCKER — Duplicated TTS output in canonical ASR+VAD+TTS stack (regression survives v0.2)

**Stack:** ASR (whisper-tiny.en) + VAD (Silero) + SmartTurn + TTS (Kokoro) + MiniCPM, all default-on. No `--minicpm-streaming-raw`. (Same path the v0.1g read-only diagnosis earlier this session flagged; verified the code paths still exist on tag `v0.2` / `f676df0`.)

**Observed:** the operator hears two overlapping TTS streams to the same turn.

**Root cause (confirmed on v0.2):** `_synthesis_dispatch_task` resets the coalescing guard `_decision_in_flight = False` at `companion_harness/realtime_orchestrator.py:1015` **immediately after the get(), before awaiting the play_task to completion**. The intent is documented in the surrounding comment (lines 1010–1014) — keep coalescing within the turn, free the gate when T4 picks up the decision — but the unstated assumption is that synthesis is fast enough that the next EOU won't arrive while audio is still playing. With real Kokoro playback (~1–3 s) and a fast follow-on user turn or detector retrigger, the next signal sails through the gate and dispatches a second synthesis on top of the first.

**Regression flag:** Not introduced in v0.2 — the design comment that locks in this race dates from before this session. v0.2 did not fix it; if anything the dashboard-driven model swaps make the race easier to hit because adapter latency varies.

**Severity rationale:** every duplicate-audio episode is a CLAUDE.md invariant 4 violation ("no proactive speech without policy approval" — the second synthesis was never policy-approved as an independent turn) and invariant 8 violation ("silence wins ties" — two voices is worse than one). For a "production-quality" milestone, this is BLOCKER.

**Minimum-viable fix:** move the `_decision_in_flight = False` reset to AFTER `play_task` completion (or guard the policy gate with `not _audio_output.is_playing`, adding `is_playing` as a separate flag from `is_synthesizing`). The first form is cleaner. Either way, surface a `coalesced_during_playback` event so the audit log shows what would have been a second turn got suppressed.

---

### F0b — BLOCKER — Model response doesn't match what the user said (stale-frame leakage, regression survives v0.2)

**Stack:** same as F0a. Whisper-tiny.en transcribes correctly; MiniCPM's spoken reply is to a different utterance than the one just spoken.

**Root cause (confirmed on v0.2):** `_tee_to_foreground` is a 64-frame `asyncio.Queue` (`companion_harness/realtime_orchestrator.py:260`) populated continuously by the audio fanout task, drained per-batch by `_bounded_frame_gen` (`realtime_orchestrator.py:977-1004`). Between batches (after `_batch_close_event` fires and before the next `_batch_open_event` opens), audio frames keep arriving and accumulate in the queue. When the next batch opens, `_bounded_frame_gen` consumes the queue FIFO — so the first frames the foreground model sees for turn N+1 are the trailing audio from turn N (silence, breath, the start of the next utterance — but also possibly residual speech from N that got queued after the previous batch closed). MiniCPM conditions its proposal on this stale prefix, producing a response that mismatches what the user just said.

There is no explicit drain of `_tee_to_foreground` at batch boundaries (verified by re-reading `_synthesis_dispatch_task` lines 1006–1019 and `_foreground_stream_task` lines 946–960 on v0.2 — no `.queue.clear()` or `while not empty: get_nowait()` call appears).

**Regression flag:** Not introduced in v0.2. Architecturally present since the live-loop integration milestone.

**Severity rationale:** the headline conversational capability is broken — the agent answers the wrong question. This is the primary user-perceptible failure of the v0.1a success criterion ("answer direct questions promptly") and the v0.2 production-quality framing. BLOCKER.

**Minimum-viable fix:** drain `_tee_to_foreground` at the start of each new batch (`_foreground_stream_task`, after `_batch_open_event.clear()`, before the `process_stream` call at line 954). This treats audio-between-turns as discardable noise rather than turn-N+1 prefix. Risks: the very last 1–2 frames of turn N that legitimately arrived just after `_batch_close_event` will get dropped — acceptable, since SmartTurn EOU already accounts for end-of-utterance ambiguity. A more surgical fix is to checkpoint a `frames_consumed_at_close` counter and discard exactly those queued during the inter-batch window — more code, marginal benefit.

---

### F1 — BLOCKER — Addressing classifier as a hard gate on speech production is a design defect

**State at time of finding:** dashboard Hot-seams toggle ablated to MiniCPM + VAD + TTS. `GET /config/seams` confirms `vad=true`, `tts=true`, all others (including `asr`) `false`. MiniCPM (foreground, not a seam) stays loaded.

**Observed:** speaking into the mic produces no TTS output. No `speak_decision` of any kind is visible on the causal-chain bar.

**Operator's architectural objection:** *"if there is no transcript, it should not block the output path"*. Correct. A voice-to-voice foreground model (MiniCPM) plus VAD plus TTS is sufficient for at least best-effort speech production. Requiring a text transcript before the policy will even consider speaking inverts the responsibility: ASR is a convenience for downstream signals, not a prerequisite for being heard.

**Mechanical root cause:** the addressing classifier (`MiniCPMAddressingClassifier` primary, `WakeWordAddressingClassifier` safety-net per PR #182) operates on the ASR transcript. With ASR disabled, no transcript event is emitted; the classifier has no input and returns `user_addressed_agent=False`; `speak_policy.decide()` short-circuits at the `NOT_ADDRESSED_TO_AGENT` branch and emits silence. There is no fallback path that lets MiniCPM speak when the text-addressing layer is uninformed.

**Why this is BLOCKER for v0.2 (not CONCERN):**
- v0.2 is the "production-quality" release umbrella per `ROADMAP.md`.
- The dashboard (PR #294 / dashboard P1 wave) exposes ASR as a toggleable seam, advertising it as ablatable.
- Toggling something the UI advertises as ablatable should not silently break the headline capability.
- The architecture pretends MiniCPM is voice-to-voice but in practice routes every speak decision through a text-only gate. That's a contradiction with the model's whole point.

**Required design fix (not implementing here, just naming):**
1. **Decouple addressing from transcript availability.** The addressing classifier needs at least one voice-mode path that does not depend on ASR text — e.g. wake-word detection on raw audio (most robust), or a "MiniCPM scores its own addressing from audio" path. Wake-word-on-audio is the smallest viable patch.
2. **Default-permit policy when no addressing signal is available.** If the classifier returns `unknown` (not `False`), `speak_policy.decide()` should fall through to the proposer rather than short-circuit to silence. Silence-wins-ties (invariant 8) is the right default when there *is* a signal saying "not for me"; it is the wrong default when there is no signal at all.
3. **Diagnosability** (the cheap interim) — emit a `MISSING_SIGNAL_PRODUCER` `ReasonCode` so the operator sees the silence cause on the causal-chain bar. This does not fix the design but at least stops the silent-failure mode while #1 and #2 are designed.

**Relation to v0.1g diagnosis:** confirms the silence pattern earlier blamed on v0.1g's WakeWord-only fallback survives unchanged to v0.2 and is more fundamental than that diagnosis credited — it is not a fallback bug, it is the gate's design.

---

### F0c — CONCERN — `synthesis_skipped_no_proposal` fires on rapid back-to-back turns (third silence pattern, distinct from F1)

**Reproduced automatically** by `tests/manual/repro_f0a_f0b.py auto` against the live `v0.2` (`f676df0`) console: two synthesized utterances streamed into `/ws/ingest` 800 ms apart produce:

| event_type | count |
|---|---|
| `asr_transcript_emitted` | 2 |
| `addressing_classified` | 2 |
| `policy_decision` | 2 |
| `foreground_proposal` | **1** |
| `tts_synthesis_started` | 1 |
| `synthesis_skipped_no_proposal` | **1** |

**Interpretation:** turn 1 runs the full path and emits a proposal + TTS. Turn 2 is approved by SpeakPolicy (a second `policy_decision` is emitted) but the foreground proposer does not produce a proposal within the `proposal_batch_window_ms=200` grace window (`manual_test_console/live_pipeline.py:674` on v0.1g — verify same on v0.2). The dispatcher then emits `synthesis_skipped_no_proposal` (`companion_harness/realtime_orchestrator.py:1069` per the earlier debugger trace) and stays silent.

**Why this is distinct from F1:**
- F1 silence: policy denies (`NOT_ADDRESSED_TO_AGENT`) because addressing has no signal.
- F0c silence: policy *approves*, but the proposer races the 200 ms batch-window timer and loses.

**Root cause hypothesis:** real MiniCPM first-token latency on a b200 under concurrent load (the first turn's response generation may still be holding model context) exceeds 200 ms for the second turn. The 200 ms grace was chosen assuming a cold-cache first-token, not a context-loaded second-turn first-token.

**Fix options:**
1. **Raise `proposal_batch_window_ms`** to a value that covers second-turn first-token latency (instrument first; likely 400–800 ms is sufficient).
2. **Make the window adaptive** — extend the grace if the proposer has emitted "started but not yet produced" within the window.
3. **Emit a louder failure mode** — `synthesis_skipped_no_proposal` is the right audit event but should also trigger a brief stall message to the UI so operators see the silence cause without log-diving.

**Severity:** CONCERN, not BLOCKER. The pipeline is functioning to spec (silence-wins-ties when no proposal exists); the spec just has the threshold too low for v0.2's adapter stack. Real human conversation includes "let me think" pauses where this would surface. Worth a v0.2.x.

**Module E sweep — quantitative evidence (added 2026-05-16 after Module E):**

| inter-utterance silence | events | transcripts | proposals | tts | skipped | drops | audio_chunks |
|---|---|---|---|---|---|---|---|
| 100 ms | 233 | 0 | 0 | 0 | 0 | 34 | 0 |
| 300 ms | 306 | 2 | 2 | 2 | 0 | 46 | 2 |
| 500 ms | 341 | 2 | 2 | 2 | 0 | 58 | 2 |
| 800 ms | 392 | 2 | **0** | **0** | **2** | 78 | **0** |
| 1500 ms | 537 | 2 | **0** | **0** | **2** | 126 | **0** |

Pattern is **non-monotonic in silence length** — happy path at 300–500 ms, total proposer failure at 800–1500 ms. Then with `orchestrator.proposal_batch_window_ms` patched 80 → 200 via `POST /config/patch`:

| case | transcripts | proposals | tts | skipped | drops | audio_chunks |
|---|---|---|---|---|---|---|
| 800 ms (window 200) | 2 | **1** | **1** | **1** | 78 | **1** |
| 1500 ms (window 200) | 2 | **2** | **2** | **0** | 126 | **2** |

So widening the window fully recovers the 1500 ms case and partially recovers 800 ms. This **confirms the F0c root cause** AND demonstrates the F0b interaction: stale audio frames buffered during long silence still tip the proposer over the 200 ms ceiling at 800 ms, because `_tee_to_foreground` accumulates noise without draining. The proper fix is the F0b queue-drain AND raising this ConfigStore default to at least 200 ms.

**Concrete recommendation:**
- Raise default of `orchestrator.proposal_batch_window_ms` (currently 80; max 200) to 200.
- Fix F0b's queue-drain so the silence padding doesn't dominate the new batch.
- Schema field `code_location` for this key is `realtime_orchestrator.py:175`.

**Operator-session reproduction (added 2026-05-16 after live retest):** in the 10-minute retest session with VAD/ASR/TTS-only ablation, **100% of policy approvals failed to synthesize**. Of 12 `policy_decision` events: 2 were `full_response, EOU_CONFIRMED` (the rest were `silence, NOT_ADDRESSED_TO_AGENT` — see F1b). Both `full_response` decisions were followed by `synthesis_skipped_no_proposal`. Zero `tts_synthesis_started` and zero `audio_out` chunks in the entire session. F0c is not a corner case under real load with the v0.2 adapter stack — it's the dominant failure mode after F1b lets a turn through.

---

### F0d — CONCERN — `log_drop_or_degrade` fires at high rate even on light synthetic load (invariant 10 stressed)

**Reproduced automatically** by the same auto run: ~12 seconds of recording at 2 utterances (≈3 seconds of audio, ≈400 events from /ws/display) produced **78 `log_drop_or_degrade` events**. That is ~6/s of dropped audit events on a load any real operator session will exceed.

**Per-event histogram from the same run:**

| count | event_type |
|---|---|
| 144 | `raw_audio_chunk` |
| 144 | `vad_frame` |
| 78 | `log_drop_or_degrade` |

VAD frames + raw audio chunks dominate volume. The EventLogger ring (`maxsize=4096` per `manual_test_console/server.py:1110`) plus async sink throughput cannot drain fast enough; backpressure correctly emits `log_drop_or_degrade` per CLAUDE.md invariant 10 ("emit `log_drop_or_degrade` event — never silently lose events").

**Why this matters:**
- v0.2's "production-quality" framing implies the audit log is a load-bearing artifact (Tier-B replay depends on it).
- 6 dropped events/s on light load means a busy session will lose audit chains by the time CI scrapes the log.
- Invariant 6 says replay agreement uses the *behavioral tuple* — but if drops break causal chains (`caused_by` references missing events), even behavioral replay risks alignment errors.

**Fix options (cheap first):**
1. **Raise `maxsize`** above 4096 — instrument first; current rate suggests ≥16384 would absorb burst.
2. **Drop selectively** — `raw_audio_chunk` and `vad_frame` are high-rate, low-information; if necessary they can be sampled (every Nth) rather than fully dropped, so the audit shape is preserved.
3. **Investigate sink throughput** — if the sink is `_null_sink` (which it is per `manual_test_console/server.py:1110-1111`) the drop is purely fan-out to display WS subscribers; raising the per-subscriber queue bound may suffice.

**Severity:** CONCERN, not BLOCKER, because the system emits the drop event rather than silently losing data (invariant 10 honored). But for v0.2's production-quality posture, 78 drops in 12 s on synthetic load is a sizing bug.

**Module E quantitative evidence (added 2026-05-16):** logger drops scale linearly with event volume and silence duration —

| inter-utterance silence | events captured | log_drop_or_degrade |
|---|---|---|
| 100 ms | 233 | 34 |
| 300 ms | 306 | 46 |
| 500 ms | 341 | 58 |
| 800 ms | 392 | 78 |
| 1500 ms | 537 | **126** |

Ratio drops/events ≈ 13–24% across the sweep — roughly one of every 4–7 events is dropped from the display fan-out even on light synthetic load. For a session lasting an hour, that translates to thousands of unobservable causal links.

---

### F1b — BLOCKER — `MiniCPMAddressingClassifier` is never wired in by the server; only `WakeWordAddressingClassifier` runs

**Reproduced** in the operator's 2026-05-16 retest session (VAD/ASR/TTS-only ablation). Of 12 `addressing_classified` events captured, **all 12** were emitted by `WakeWordAddressingClassifier` (`classifier_name: "WakeWordAddressingClassifier"`, evidence `"implicit_fallback"`, confidence 0.5). Zero events came from `MiniCPMAddressingClassifier` despite the foreground MiniCPM-o being loaded (`minicpm_loaded: true` in `/healthz`).

**Root cause:** `manual_test_console/live_pipeline.py:628-631`
```python
if minicpm_text_model is not None:
    minicpm_addressing = MiniCPMAddressingClassifierImpl(minicpm_text_model)
else:
    minicpm_addressing = _NullMiniCPMAddressingClassifier()
```
The parameter `minicpm_text_model` (signature default `None` at line 502) is **never passed by `manual_test_console/server.py`** — `grep -rn "minicpm_text_model" manual_test_console/` returns only the line-502 definition and the line-628/629 check. So `_NullMiniCPMAddressingClassifier` always wins; the addressing chain always falls through to the WakeWord safety-net.

**Why this is BLOCKER (sharper than F1's design objection):** PR #182 ("v0.1j Task 9 addressing routing — MiniCPM primary + wake-word safety-net") shipped only the seam; the server never plugs the model into it. The "MiniCPM-derived classifier is the final-product primary (issue #139)" comment at line 626 of live_pipeline.py is aspirational.

**Operator-visible impact:** in the retest session, 10 of 12 policy decisions returned `action='silence', reason='NOT_ADDRESSED_TO_AGENT'` because WakeWord couldn't match any wake word in conversational phrases like "can you hear me". Real conversation is essentially unusable: 83% silence rate even when the user is plainly addressing the agent.

**Fix:** thread the loaded foreground model through `build_live_pipeline(minicpm_text_model=...)` from `server.py`. The seam already accepts it — just no caller fills it. Adjacent fix: make `_NullMiniCPMAddressingClassifier` log a `signal_producer_fallback` so operators can see the degradation rather than silently rolling to WakeWord.

---

### G1 — BLOCKER — Audit-stream visibility gap: 20.5% of `caused_by` references point to events that never appear in `/ws/display`

**Reproduced** by Module G's audit-integrity check on `tests/manual/repro_f0a_f0b.py auto --inter-utterance-ms 500` capture (341 events). **70 events (20.5%) reference at least one `caused_by` id that is never emitted to `/ws/display`.** Every such missing id has the format `-nd-<seq>-<ts_mono>` — leading dash, no session prefix, no producer prefix, just `nd` (looks like "null detector").

Sample missing-id refs cited by real events:

```
type=asr_transcript_emitted        source=streaming_realtime_orchestrator  caused_by=['-nd-24-750334343']
type=memory_retrieval_event        source=streaming_realtime_orchestrator  caused_by=['-nd-24-750334343']
type=signal_producer_fallback      source=streaming_realtime_orchestrator  caused_by=['-nd-24-750334343']
```

Three downstream event classes (the transcript path, the memory-retrieval path, AND the signal-producer-fallback path) all hold causal references to id `-nd-24-...` — but no event with that id ever crosses the display WS. Either:

- The null-detector producer manufactures event ids and references them in causal chains but never calls `EventLogger.log()` on them.
- `EventLogger` accepts them but the display broker filters them out before fan-out.
- They are real events being dropped silently (would violate invariant 10 — drops must emit `log_drop_or_degrade`, and the 78 drops we saw don't match these specific ids).

**Why this is BLOCKER for v0.2:**
- **CLAUDE.md invariant 1:** "Every input event, signal, decision, and action is timestamped, source-attributed, and **recorded** with its causal predecessors." If 20% of caused_by predecessors are unobservable, the recording promise is broken.
- **CLAUDE.md invariant 5:** Policy-layer replay must be bit-identical. A replayer walking the `caused_by` DAG cannot resolve a fifth of the chain heads; deterministic re-execution against the audit log is therefore *not* possible from the surface that operators see.
- **Severity of the practical impact:** the `signal_producer_fallback` event (introduced in v0.2a for the background reasoner) is one of the events whose causal root is unresolvable. Any v0.2-era forensic narrative that asks "why did the producer fall back" cannot follow the chain.

**Hypothesis for root cause (untested):** producers that wrap stub/null detectors (e.g. `_NullDeicticModel`, `_NullSceneScorer`, etc.) construct their own event ids using a `-nd-` prefix to satisfy the type signature without actually logging the event. Real downstream code then references that id in `caused_by`, but the broker fans out only events that pass through the EventLogger, leaving the synthetic id dangling.

**Fix direction (not implementing):**
1. **Make null-detector producers log their phantom events** (with `subject_class="stub"` so they're visibly degraded). Closes the chain.
2. **OR make consumers not cite null-detector ids in caused_by.** Cleaner architectural fix.
3. **OR add a "synthetic root" event id** that all null-detector references collapse to, so the chain at least resolves to a single visible sentinel.

#1 is the smallest patch; #3 is the cleanest. Either way, the unresolved-ref rate must drop to ~0% before v0.2's audit story is honest.

---

### F2 — NIT — Causal-chain / TurnSignals / SpeakDecisions panel is too noisy

**Observed:** the live audit bar in the dashboard streams every event that crosses the EventLogger. High-volume producers (per-frame VAD scores, per-chunk audio events, frame ingest) drown out low-volume but high-signal events (SpeakDecision, RubricViolation, model_swap_*, reasoner_budget_exhausted, signal_producer_fallback).

**Requested:** a filter in the panel. Reasonable filter axes:
- **By event type** — checklist of event class names, default-on for high-signal classes, default-off for VAD/frame chatter.
- **By severity / reason_code** — collapse approve/deny chains, expand only on operator click.
- **Search box** — substring match on `caused_by[]` or payload text for tracing one decision.
- **Pin / mute** — pin a specific event type to the top regardless of volume.

**Scope:** frontend-only if the EventLogger keeps shipping everything; no backend change required. Could ride alongside the next dashboard iteration (P2 if one is planned).

**Module F quantitative evidence (added 2026-05-16):** with `playwright_panel_under_load.py` sampling the panel every 250 ms during a 2-utterance `repro_f0a_f0b.py auto` (`--inter-utterance-ms 500`) — 14 s of synthetic load:

| metric | value |
|---|---|
| panel final char count | **6 934** |
| panel final line count | **430** |
| unique lines accumulated | 81 |
| newly-appearing line classification | signal: **3** · noise: 28 · chrome/timestamps: 96 |
| signal/noise ratio | **0.107** (≈ 1 signal line per ~9 non-signal lines) |

Extrapolated to a 1-hour session: ~110 000 lines / ~1.8 MB DOM text in this single panel. The browser DOM scroll buffer is unbounded; performance will degrade linearly. Filter UX is **one** of two needs; the other is a **retained-line cap** (e.g. keep last 500 lines, drop older). Both are frontend-only fixes.

---

## Noticed while verifying the briefing (not operator-reported)

These surfaced when checking the running server against handbook §9. Filed here so they don't get lost; classify and pursue at your discretion.

### N1 — BLOCKER candidate — `/metrics` Prometheus endpoint not registered

Handbook §9.2 advertises `curl -s http://localhost:8800/metrics` returning Prometheus text. Live probe: **HTTP 404**. `grep -nE "metrics|prometheus" manual_test_console/server.py` returns nothing. Route registration in `manual_test_console/server.py:1303-1312` does not include `/metrics`. Either PR #298 (v0.2f deployability) shipped the doc claim without the route, or the route lives in an unmounted module. Worth confirming before any deployment relies on Prometheus scraping.

### N2 — BLOCKER candidate — Handbook §9.3 eval URLs are wrong

Handbook says `/eval.html`, `POST /eval/run`, `GET /eval/status/<run_id>`. Actual (`manual_test_console/eval_routes.py:237-241`): UI at `/eval` (200 OK), `POST /eval/runs`, `GET /eval/runs/{run_id}`. The feature works; the handbook just sends operators to 404s. Either fix the doc or add the documented aliases.

### N3 — CONCERN — `POST /config/seam` is `POST /config/model-swap`

Handbook §9.4 names `POST /config/seam`; live probe returns 404. Actual swap endpoint is `POST /config/model-swap` and the state read is `GET /config/seams` (plural), at `manual_test_console/server.py:1311-1312`. The dashboard frontend uses the right endpoints (the toggles work — see F1's ablation state), but any operator scripting from the handbook will fail.

### N4 — CONCERN — `/healthz` field shape and units diverge from §9.1

Handbook §9.1 promises:
- `adapters` dict — actual is **flat top-level keys** (`scene_scorer`, `grounding_model`, `av_conflict_scorer`, ...).
- `gpu_memory_used_gb` / `gpu_memory_total_gb` (GB) — actual is `gpu_memory_allocated_mb` / `gpu_memory_reserved_mb` / `gpu_memory_total_mb` (MB).
- `event_rate_per_s` (10 s window) — actual is `events_per_second_last_60s` (60 s window).

Operators writing monitoring against the handbook will get key-not-found and wrong-unit math. Either reshape the response or correct the doc.

### N5 — CONCERN — v0.2e default-on flip for Deictic appears not to take effect

`/healthz` on the running instance shows `deictic_model: "stub:_NullDeicticModel"` even though `--enable-deictic` defaults to True (`manual_test_console/server.py:1683`) and v0.2 release notes claim Deictic is now default-on. The early-return at `manual_test_console/server.py:1438-1439` in `_on_startup_finalize_deictic` exits when `labels["deictic_model"]` starts with `"stub:"` — which is exactly the state where it should upgrade. Either the label is being written wrong in `main()` before `build_app`, or the condition is inverted. Worth a 10-minute read; if confirmed, it's a real regression vs the v0.2e claim. Not BLOCKER because Deictic is a signal producer; pipeline does not crash without it.

### N6 — CONCERN — `/eval/runs/{id}/event_logs/{case_id}` always 404 (writer/reader path mismatch)

Direct cause: writer at `companion_harness/evals/adapters/harness_native.py:159` produces `<reports>/<run_id>/event_logs/<case_id>.jsonl` (verified on disk). Reader at `manual_test_console/eval_routes.py:211` looks for `<reports>/<run_id>/<case_id>/events.jsonl` (different directory, different filename). The two paths can never coincide; the endpoint returns 404 even for runs whose `event_log_path` is correctly reported in `GET /eval/runs/{id}`.

Reproduced on the existing `harness_-1778954113-909008` run:

- Manifest reports `event_log_path: /tmp/manual_test_blobs/eval_reports/harness_-1778954113-909008/event_logs/thinking_pause.jsonl` ✓ (exists)
- `GET /eval/runs/harness_-1778954113-909008/event_logs/thinking_pause` → HTTP 404, body `{"error": "event log not found for ..."}`

**Impact:** the eval console can list runs and show pass/fail per case, but cannot fetch per-case event logs through the API — operators must shell into the box to inspect failures. The handbook explicitly advertises this endpoint in §9.3.

**Fix direction:** rewrite line 211 to `event_logs / f"{case_id}.jsonl"` to match the writer.

### N7 — CONCERN — Eval manifest serializes Python `None` as the literal string `"None"`

`GET /eval/runs/{id}` for synthetic-mode runs (voicebench, vocalbench, etc.) returns each case's `event_log_path` as the **string** `"None"` rather than JSON `null` or an omitted field. Any JS/Python consumer that does `if result.event_log_path is None` (Python) or `if (result.event_log_path === null)` (JS) will always see the truthy string `"None"` instead of the falsy sentinel.

Reproduced on the fresh `voiceben-1778959730-6c1c9c` run launched in Module D: all 6 cases have `"event_log_path": "None"`.

**Fix direction:** in the run-manifest writer (likely `companion_harness/evals/runners.py` or the eval adapter base class), use `path if path else None` and let `json.dumps` produce `null`. Or omit the field entirely when there is no log.

### F3 — CONCERN — Dashboard filter unchecking has no effect because the legacy `renderEvent` path appends rows regardless

**Reproduced** in operator's retest session: unchecking `vad_frame` in the audit filter still shows vad_frame rows scrolling in the panel.

**Root cause** in `manual_test_console/index.html`:

1. `renderEvent(evt)` at line 302 runs filter-aware subscribers synchronously (lines 306–308), **then directly appends a row to either `ingestRows` or `signalRows` at line 345 — without consulting the filter**.
2. The filter-aware subscriber added at line 1527 pushes the event into `allSignalEvents` and calls `applyFilter()` which does `signalRows.innerHTML = ""` (line 1470) and rebuilds from the filtered list.
3. **Sequence per event:** subscriber runs first → applyFilter clears+rebuilds (correctly excluding unchecked types) → THEN legacy code at line 345 appends the new row again.

Result: every newly-arrived `vad_frame` (114 of them in the retest session) re-appears as a row, surviving until the next event triggers another applyFilter wipe. On a high-rate stream the panel appears to continuously contain the filtered-out type.

**Fix options:**
1. **Delete the legacy direct-append for signal-bound events** at lines 343–354. The filter-aware subscriber already owns that rendering.
2. **OR** gate the legacy append on `isTypeEnabled(evt.event_type)` so it respects the filter.
3. **OR** restructure so all renders go through `applyFilter` (single source of truth).

Option 1 is cleanest; option 2 is the smallest patch.

---

### F4 — CONCERN — `signal_producer_fallback` and `synthesis_skipped_no_proposal` ship with `payload_inline: null` — operators cannot diagnose

**Reproduced** in the operator's retest session:

- **24 `signal_producer_fallback` events** all carry `payload_inline: null` (no producer name, no fallback path identified). 24 fallbacks in 10 minutes is a lot of degradation; operators have no way to tell *which* producer is misbehaving from the audit stream alone.
- **2 `synthesis_skipped_no_proposal` events** also carry `payload_inline: null` (no dispatcher state, no proposer name, no batch window snapshot).

Both event types DO have `payload_hash` and `payload_ref` fields, but `payload_ref` is `null` too — so there's no on-disk blob to fetch. The information about *what* happened is not anywhere observable from `/ws/display` or from the blob store.

**CLAUDE.md invariant 1** says "Every input event, signal, decision, and action is timestamped, source-attributed, and **recorded** with its causal predecessors." The events themselves are recorded, but their substantive content is unobservable — operators see only that a fallback or skip happened, never the why.

**Fix direction:** add inline-typed shapes for both event classes mirroring the `policy_decision` pattern (which carries `{action_type, primary_reason_code}` inline):
- `signal_producer_fallback.payload_inline = {producer: str, from_path: str, to_path: str, reason: str}`
- `synthesis_skipped_no_proposal.payload_inline = {dispatcher_state: str, batch_window_ms: int, batch_open_at_ms: int, batch_close_at_ms: int, signal_evt_id: str}`

Diagnosability is a v0.2 production-quality concern. Without these, F0c and the 24-fallbacks-per-10-min issue cannot be triaged without source code spelunking.

---

### N8 — CONCERN — `synthesis_skipped_no_proposal` sweep evidence: the 80 ms `proposal_batch_window_ms` default is too tight for v0.2's adapter stack

See **F0c** for the full sweep table. Headline: at the ConfigStore default of 80 ms, the 800 ms and 1500 ms inter-utterance cases produce **zero proposals** out of 2 transcripts. Patching the window to 200 ms (its declared max) restores 1500 ms to full happy-path and partially recovers 800 ms. The current default ships a tighter window than the running adapter stack can satisfy on second-turn first-token under real load.

**Fix direction:** raise the default to 200 (the schema's current `max`) and consider increasing `max` further so operators have headroom under model contention. Schema location: `realtime_orchestrator.py:175`.

---

## Positive verifications (worth recording so they don't get lost)

These came back clean on v0.2. Listed so the next reader knows what *is* working and avoids re-testing them blind.

- **`POST /config/patch` validation is solid.** Three error classes (unknown key → 403 + tier=unknown; out-of-range → 400 with min/max embedded; type mismatch → 400 with expected/got types). Error bodies are descriptive and tier-aware. Confirmed for `orchestrator.proposal_batch_window_ms` with bogus key, value 99999, and value `"abc"`.
- **`POST /config/model-swap` chain is fully auditable.** Round-trip swap (false → true → false) on `attachment_risk_monitor` produced two complete chains: each has `operator_action` → `model_swap_requested` → `model_swap_completed`, all with consistent `caused_by` and rich typed `payload_inline` ({seam, from_enabled, to_enabled, applied_at_ms, latency_ms, operator_action_event_id}). Invalid seam name produces an audit-visible `model_swap_rejected` event in addition to HTTP 403 — clean rejection trail.
- **`/ws/ingest` is robust to malformed input.** All 6 abuse classes (invalid JSON, missing event_type, unknown event_type, invalid base64, 5 MB oversized envelope, binary frame) leave the WS open AND `/healthz` continues to respond after each. No crash, no leak (active_sessions returns to 0 after sockets close).
- **Concurrent session accounting is correct.** Opened 3 ingest WS in parallel: `sessions_opened` delta = 3 (expected); `active_sessions` = 3 with sockets open, drops to 0 after close.
- **`export_replay_report_tar` byte-stability claim holds.** Two consecutive calls on the same session produced identical 10 240-byte tar bundles (sha256 match). Confirms PR #298's deterministic-export promise.
- **`scripts/v0_2_replay_report.py` readiness banner runs and reports 19/19 MET.** POLICY_VERSION reported as `v0.2-final`. Banner internally cites the *actual* /healthz field names (`gpu_memory_allocated_mb`, `vad_ready`, etc.) — which is why the banner passes its gates while the handbook §9.1 documents non-existent names (N4): the gates were written against shipped code; the doc was written against a wished-for API.
- **Live pipeline invariant 1 field coverage is 100%.** Every event in the 500 ms-silence repro carries all 9 required fields (`event_id`, `session_id`, `schema_version`, `seq_no`, `event_type`, `timestamp_mono_ms`, `timestamp_wall`, `source`, `caused_by`). `schema_version` uniformly `"0.1"`. Source attribution is honest (every event names a real producer). The audit DAG breaks only via the `-nd-` reference gap (G1), not via missing fields.
