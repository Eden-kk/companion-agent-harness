# Manual-test findings — 2026-05-15

Session run by the project lead against the harness on b200 (commit `3be3aea`, Phase 1 + Phase 2 + handbook merged; live-loop pipeline subsequently wired during the session via a "Phase 3" server build — see startup banner in `/tmp/manual_test_server.log`). Test companion: a fresh Claude Code session following `docs/manual-test-session-brief.md`.

**Topology note.** This test-companion session was running directly on b200 (`mlsys-b200.ucsd.edu`), not on a separate orchestration host. The brief's prohibition on SSH-ing to b200 was written assuming a different topology; with the lead's explicit permission, the test companion read `/tmp/manual_test_server.log` and `/tmp/manual_test_blobs/decision_traces/` directly. The server was not modified or restarted.

The session covered the silence baseline (scenario A) and addressed-agent (scenario B). Scenarios C–L were not exercised because Finding 6 made it clear the system cannot reach any `full_response` outcome in the rig as it stands.

---

## Finding 1 — Phase 1 server initially shipped without VAD / SpeakPolicy / orchestrator wired

- **Scenario:** pre-flight (before the orchestrator was integrated)
- **What I saw:** Initial build had only `InputIngest` wired in `manual_test_console/server.py:build_app()`; zero hits for VAD/SpeakPolicy/RealtimeOrchestrator imports. Left panel showed `raw_audio_chunk`; right panel showed only `harness_init` / `session_open`.
- **What I expected:** Per handbook §2.4, four lanes including VAD signals and SpeakDecisions.
- **Severity:** bug / scope mismatch.
- **Resolution mid-session:** lead pulled in a "Phase 3" build that loads MiniCPM-o + real detectors at startup; banner reads "live loop: ENABLED" and "sessions: VAD, SmartTurn, Backchannel, SpeakPolicy, MiniCPM proposals". Findings 3–6 describe behavior under that build.
- **For orchestration session:** decide whether the Phase 3 server build is what `manual_test_console/server.py` on `main` should be (i.e., land it as the production entry point), or whether the handbook should declare Phase 1 as "ingest-only" and reference Phase 3 explicitly. Right now the on-main code and the running server disagree on scope.

---

## Finding 2 — Confusion-only: `raw_audio_chunk` rows route to the left panel, not the right

- **Severity:** confusion (not a bug — `renderEvent` in `manual_test_console/index.html` correctly routes `payload_kind == raw_audio|raw_video` to `ingestRows` and everything else to `signalRows`).
- **Suggested fix:** Annotate handbook §2.4 with which `event_type`s go to which panel, or add a per-event-type strip header inside each panel.

---

## Finding 3 — `log_drop_or_degrade` storm during silence (orchestrator-wired build)

- **Scenario:** A (silence) after orchestrator integration
- **What I saw:** Right panel emitted ≥10 consecutive `log_drop_or_degrade` rows during silence, interspersed with `policy_decision`, `smart_turn_signal`, `backchannel_classification`. Server log shows MiniCPM-o foreground model loaded and the pipeline running.
- **Severity:** bug. Invariant #10's "emit on backpressure" path is firing correctly, but backpressure during silence implies the EventLogger drain can't keep up with the orchestrator's idle event rate. The audit trail is preserved (no silent loss), but the manual-test console becomes a lossy view of the policy lane during normal operation.
- **For orchestration session:** quantify drops/total over a steady-state silent stretch (use `/tmp/manual_test_blobs/decision_traces/` timestamp density vs `policy_decision` events in the log). If drops are non-trivial, the EventLogger drain is the headline performance bug. Likely fix shapes: bigger queue, batch-flush in drain, or move heavy serialization off the drain loop.

---

## Finding 4 — *Superseded by Finding 6.*

The hypothesis that foreground / assistant pipeline activity during silence might indicate an over-eager invariant #4/#8 violation was wrong. The decision-trace store (588 traces) shows zero `full_response` decisions across the entire manual-test history, so `assistant_generation_start` / `assistant_audio_buffer_flushed` events observed in the left panel are speculative foreground generation that the policy gates correctly suppress (consistent with the `synthesis_skipped_no_proposal` rows also observed). No invariant violation. See Finding 6 for the actual bug.

---

## Finding 5 — Live console cannot surface `action_type` / `primary_reason_code` on `policy_decision` rows

- **What I saw:** The Event JSON shipped over `/ws/display` for `policy_decision` only carries the audit envelope; `action_type` and `primary_reason_code` live behind `payload_ref: "decision_trace://..."` which the console doesn't fetch. The decision content is on disk at `/tmp/manual_test_blobs/decision_traces/<decision_id>.json` (via `DecisionTraceStore`).
- **Severity:** bug — blocks every "is this expected?" question that depends on the policy verdict. It is the proximate reason Finding 6 took an out-of-band b200 read to confirm.
- **Suggested fix:** Either (a) inline `action_type` + `primary_reason_code` in the `policy_decision` Event envelope (cheap; both are enum values, no sensitivity concern), or (b) add a `/decision_trace/<decision_id>` HTTP endpoint to `manual_test_console.server` and a row-click handler in `index.html` that fetches and renders the trace. Option (a) is one line in the orchestrator's policy_decision emit site (`companion_harness/realtime_orchestrator.py` around line 491).

---

## Finding 6 — **Headline bug.** Harness cannot break silence: `user_addressed_agent` is never True

- **Scenario:** B (address the agent) — and by extension A, C, F, H, J, K (any scenario that depends on `action_type=full_response`).
- **What I tried:** Said "hey companion, what's the weather like?"-style utterances; checked every recent `policy_decision`'s decision trace.
- **What I saw, across all 588 decision traces in `/tmp/manual_test_blobs/decision_traces/`:**
    | action_selected | count | primary_reason_code | threshold_path |
    |---|---:|---|---|
    | `silence` | 530 (90%) | `NOT_ADDRESSED_TO_AGENT` | `alert_threshold:default:medium / eou_gate / eou_gate:passed / silence:fallthrough` |
    | `backchannel` | 58 (10%) | `BACKCHANNEL_DETECTED` | `alert_threshold:default:medium / eou_gate / eou_gate:passed / backchannel_threshold:exceeded` |
    | `full_response` | **0** | — | — |
    | `clarification` | 0 | — | — |
- **Critical observation:** `eou_gate:passed` on every silence decision. SmartTurnDetector is correctly confirming end-of-utterance. The policy *knows* the user finished speaking. It chooses silence because `user_addressed_agent` is never True, hence the `silence:fallthrough` branch.
- **What I expected:** handbook §2.5 scenario B expects `action_type=full_response` with `primary_reason_code=EOU_CONFIRMED` after the user addresses the agent.
- **Severity:** **bug — load-bearing.** The harness in its current manual-test deployment cannot speak. Scenarios B, C, D, E, F, H, I, J, K, L are all unreachable end-to-end as long as `user_addressed_agent` stays False. This is the single thing that, if fixed, would unblock the largest amount of manual-test scope.
- **Likely root cause** (for orchestration session to confirm): `user_addressed_agent` is populated by the foreground model's `proposals_to_signals` path (per handbook §2.5 scenario C). v0.1e is known to have ASR-not-wired-until-Stage-5 as a caveat (brief §4 row H). If the foreground model has no transcribed user text to classify addressing on, `user_addressed_agent` defaults to False for every utterance and the policy permanently falls through to silence. The brief listed this as "retrieval is inert in live flow" — the broader consequence is that the entire `proposals_to_signals` path that drives addressing detection is also inert in live flow, not just retrieval.
- **For orchestration session:**
    1. Confirm in `companion_harness/realtime_orchestrator.py` where `user_addressed_agent` is set — is it gated on a real ASR transcript? If so, this is a "ASR-not-wired" downstream symptom; the manual-test rig fundamentally can't exercise the speaking path until Stage 5.
    2. Decide whether to (a) ship an interim addressing heuristic for the rig (e.g., always-addressed during manual test, or volume-based addressing), (b) wire a placeholder ASR for the rig, or (c) document the limitation in the handbook and accept that B/D/E/H/J/K can only be exercised against fixtures, not live.
    3. The handbook's known-bug caveats for D/E/H assumed v0.1b/v0.1e classifier wiring was the blocker; Finding 6 says the addressing classifier itself is upstream-starved, which is a bigger problem than D/E/H individually.

---

## Session summary

- **Findings by severity:** 5 bugs (1, 3, 5, 6 headline; 4 superseded into 6), 1 confusion (2). The known-bug caveats from brief §4 (D #10, E #20, G #105, F #113, H retrieval-inert) were not reachable because Finding 6 stopped every path at the addressing gate.
- **What the orchestration session should look at first: Finding 6.** Until `user_addressed_agent` can become True in the live rig, no scenario B-onward is observable, so fixing it unblocks the largest part of the manual-test program. Findings 3 (drain backpressure) and 5 (console can't show action_type) are the close seconds — Finding 3 because it makes the rig lossy regardless of what's running, Finding 5 because it was the structural reason Finding 6 took a b200-side read to confirm rather than being readable from the live console.
- **Decision-trace evidence preserved on b200:** `/tmp/manual_test_blobs/decision_traces/` — 588 traces from across this session and prior sessions. The Phase 3 server build is still running at the time this file was written; do not `rm` the traces.
- **Specific sessions exercised this run:** `54f45cce-5a1b-4901-9b9e-2d4a3f1dc993`, `ef9c985a-0943-4505-bbac-4407f3586c0e`, `e6030b63-e9a2-4ca5-85db-a987cf0dc0f4`, `be531d4d-78b8-4c01-a75f-ef6f0b404275` (newest). All show the same NOT_ADDRESSED_TO_AGENT fall-through pattern.

---

## Late-session update — after #125–#136 landed

Mid-session, the lead pulled in a series of PRs that materially changed the deployed pipeline:
- **#125** — wired StreamingRealtimeOrchestrator + MiniCPM-o into the live pipeline.
- **#126** — replaced CPU stub detectors with real Silero / Pipecat SmartTurn v3 / whisper-tiny + lexicon.
- **#127, #132, #135** — closed the voice-back loop (Kokoro TTS adapter + audio_out WS + browser playback).
- **#133** — inlined `action_type` + `primary_reason_code` on `policy_decision` events (Finding 5 fix).
- **#134** — fixed backchannel-emit threshold to prevent EventLogger drain saturation during silence (Finding 3 fix).
- **#131** — clarified handbook §2.4 panel routing (Finding 2 fix).
- **#136** — wired faster-whisper ASR; populated `PolicyInputs.user_transcript` on EOU.

The decision-trace distribution accordingly changed from 588 traces / 0 `full_response` to 643 traces / 23 `full_response`. Finding 6 is *technically* unblocked — the system can now choose to speak — but the way it was unblocked is itself a bug, and produces a new and worse symptom: **the model speaks during silence and asks the user questions.**

### Finding 7 — `user_addressed_agent=True` is hardcoded in the live PolicyInputs builder

- **Where:** `companion_harness/realtime_loop.py:209`, inside `_signals_to_policy_inputs()`. Every `PolicyInputs` constructed by this default builder has `user_addressed_agent=True` regardless of any signal.
- **Why it matters:** The lexicon-based addressing classifier landed in #126 (`companion_harness/backchannel_asr_lexicon.py`), but `grep -rn` finds **zero call sites in the live pipeline**. Whisper transcribes (#136) and stuffs the result into `inputs.user_transcript`, but nothing consumes that transcript to compute `user_addressed_agent` — the addressing field is set *before* the transcript is even computed, to a constant.
- **Consequence:** Finding 6's "fix" was a stub, not a real wiring. Every EOU that fires now leads to `full_response` (provided not a backchannel). Silero VAD + SmartTurn v3 occasionally fires EOU on background noise / breath / keyboard — whisper-tiny hallucinates a short transcript on the noise buffer — addressing gate passes (hardcoded) — EOU gate passes — policy approves `full_response` — MiniCPM-o responds conversationally to the hallucinated/empty input — Kokoro speaks it. That's the "asks me questions during silence" symptom: conversational LLMs given ambiguous input default to "Sorry, what did you say?" / "How can I help?". The audit chain is intact; the gates are simply not gating.
- **Severity:** **bug — load-bearing.** This is the immediate next thing to fix after the Phase 3 wiring landed. Without this fix, the rig is worse than before — it now speaks invalidly instead of being silent invalidly.
- **Evidence:** Decision-trace survey 2026-05-15 evening — 23 `full_response` decisions in `/tmp/manual_test_blobs/decision_traces/`, every one with `supporting_reason_codes: ["USER_ADDRESSED_AGENT"]` and `threshold_path` ending in `user_addressed_agent:full_response`. The addressing fact is asserted in every single one. The lead reports being silent during many of these.
- **Suggested fix:** Inject the lexicon classifier into the orchestrator's `policy_inputs_builder`. The orchestrator already takes a `policy_inputs_builder` callable in its constructor (per `companion_harness/realtime_orchestrator.py:178`-ish region), so the live-pipeline factory should pass a builder that calls `backchannel_asr_lexicon.classify_addressing(inputs.user_transcript)` instead of using the hardcoded-True default. Default should arguably be `False`, not `True`, so a future omission fails closed (silence wins ties — invariant #8).

### Finding 8 — Whisper transcript is never emitted as an Event (invariant #1 violation)

- **Where:** `companion_harness/realtime_orchestrator.py:400-405`. The transcript is computed at EOU, written into `inputs.user_transcript`, used to drive `_detect_explicit_remember` and (intended) addressing classification — but it is never emitted as an Event and never stored in the decision trace.
- **Why it matters:** Invariant #1 ("no unlogged behavior — every signal that drives a decision must be recorded with provenance"). The transcript is a load-bearing signal: it determines explicit-remember intent, it *should* drive addressing, and it'll drive Stage-5 retrieval. Not logging it means operators cannot debug "what did whisper hear?" from the audit trail — which is exactly what was needed to confirm Finding 7's hallucination hypothesis.
- **Severity:** bug. It is also the structural reason this debugging cycle had to infer the whisper-hallucination part of Finding 7's chain rather than show it directly.
- **Suggested fix:** Emit an `asr_transcript_emitted` event after the `transcript = self._asr_model(...)` call, with the transcript text in a `SensitiveField` (free-text → `companion_harness` rule from CLAUDE.md), `caused_by` linking to the raw_audio_chunk window that fed it, and `payload_kind="signal"`. Also include the transcript (or a reference) in the `decision_trace` JSON so `/decision_traces/<id>.json` carries the audit context.

---

## Updated session summary (post-#125–#136)

- **Fixed during the session by PRs that landed mid-test:** Findings 2 (#131), 3 (#134), 5 (#133). The handbook update for Finding 2 added the panel-routing paragraph at §2.4. Finding 5's fix means the live console now carries `action_type`/`primary_reason_code` inline — operators will not need a b200-side decision-trace read for routine debugging anymore.
- **Still open and load-bearing: Findings 6 → 7 chain.** Finding 6 (system can't speak) is technically unblocked by the hardcoded-True stub, but the stub *is* Finding 7 (system speaks unprompted, asks questions during silence). The orchestration session should treat Finding 7 as the highest-priority follow-up, not Finding 1 anymore.
- **Still open and structural: Finding 8.** Until the transcript is on the bus, "what did whisper hear?" remains a b200-only question, and any future addressing/intent/retrieval bug will hit the same observability wall.
- **Handbook drift.** The handbook (`docs/manual-test-handbook.md`) was written for a Phase-1 / Phase-2 audio-only / audio+video rig with no voice-back. Phases 3+ (#125, #135 voice-back, #136 ASR) are now deployed. The handbook does not yet describe these capabilities, does not name the new event types (`asr_transcript_emitted` when emitted, KokoroTTS output flow, audio_out WS), and does not warn operators that scenarios B/C now produce real spoken responses. The handbook needs a Phase-3 section before the next manual-test pass.
- **Decision-trace evidence preserved on b200:** `/tmp/manual_test_blobs/decision_traces/` — 643 traces. Server is still running at the time of this write.

---

*Findings file written by the test-companion session 2026-05-15. Brief: `docs/manual-test-session-brief.md`. Handbook: `docs/manual-test-handbook.md`. Decision-trace evidence: `/tmp/manual_test_blobs/decision_traces/*.json` on b200.*

---

# Manual-test findings — 2026-05-15 (Round 2, evening session)

Second test-companion run on 2026-05-15, after PRs #125–#149+ had landed and after the orchestration session had addressed Findings 2/3/5 and stubbed Finding 6 via the `AddressingClassifier` wiring (PR #149: wake-word + diarization-derived `social_mode` + mechanical fallback). Server confirmed running with **all real models loaded, no stubs**:

```
VAD: Silero (ONNX) · SmartTurn: Pipecat v3 · Backchannel: whisper-tiny+lexicon
ASR: whisper-tiny.en (faster-whisper) · TTS: Kokoro-82M-ONNX
Foreground: MiniCPM-o 4.5 (b200 GPU)
live_pipeline_enabled: true · audio_out_enabled: true · use_stubs: false
```

Decision-trace store cleared from prior round (operator-managed memory hygiene per handbook §2.7). Two fresh sessions in this round (`b6f22b26-…` then `e4729fa9-…` after a page reload).

This round used a **live event watcher** (`/tmp/manual_test_watcher.sh` streamed via the Monitor tool) so every `decision_trace` write was surfaced in near-real-time as it landed. That visibility was what made Findings 9–11 noticeable inside the session rather than only after-the-fact.

---

## Confirmed fixed (since previous round)

- **Finding 2** (panel routing) — handbook §2.4 now has the panel-routing paragraph (PR #131). ✓
- **Finding 3** (`log_drop_or_degrade` storm during silence) — PR #134 raised the backchannel `emit_threshold` to 0.3. Watcher logged **zero** `log_drop_or_degrade` lines this round. ✓
- **Finding 5** (live console can't surface `action_type` / `primary_reason_code`) — PR #133 inlined both fields on `policy_decision` Event envelopes. ✓
- **Finding 6** (`user_addressed_agent` never True) — PR #149 wired `AddressingClassifier` (wake-word "Claude"/"Claudia" + diarization social_mode + solo-fallback). Verified live: first `full_response` decision of the round fired within seconds — `action=full_response`, `reason=EOU_CONFIRMED`, `supporting=USER_ADDRESSED_AGENT`, `path=eou_gate:passed/user_addressed_agent:full_response`. ✓ — but see Finding 9 for the way it broke other things.

---

## Finding 9 — Orchestrator dispatches duplicate `policy_decision`s at single turn boundaries

- **Scenario:** B (address-the-agent), follow-on activity
- **What I saw:** Decision-trace dump from session `b6f22b26`:
    | sro | ts (mono ms) | gap from prev |
    |---|---:|---:|
    | 6 | 698611466 | — (first decision) |
    | 11 | 698654810 | +43344 ms |
    | 16 | 698673744 | +18934 ms |
    | 21 | 698673744 | **+0 ms** (identical ts, different seq) |
    | 26 | 698691145 | +17401 ms |
    | 32 | 698698027 | +6882 ms |
    | 37 | 698698028 | **+1 ms** |
    | 42 | 698698028 | **+0 ms** |
  Every row is `action=full_response / reason=EOU_CONFIRMED / supporting=USER_ADDRESSED_AGENT / path=…/user_addressed_agent:full_response`. The pairs/triples at identical `timestamp_mono_ms` with different `seq_no` are the bug signature — the orchestrator is producing two or three policy decisions for what should be a single turn boundary.
- **What I expected:** Per spec, one policy decision per EOU. Multiple decisions at bit-identical timestamps would also be a soft hit against invariant #5 (replay determinism) — replay would need to reconstruct the dispatch order deterministically, and if the orchestrator is racing internally to dispatch them, that ordering may not be stable.
- **Severity:** **bug.** Each duplicate `full_response` triggers another Kokoro TTS synthesis (see Finding 10 for what happens to that audio). Combined with the audio path coming back through the mic when speakers are on, this becomes the "model talks over itself, asks questions during silence" symptom the operator experienced.
- **Known issue ref:** new (does NOT supersede prior Finding 7 — that one was about hardcoded `user_addressed_agent`, which PR #149 fixed by wiring the classifier; Finding 9 is a separate failure in dispatch).
- **Hypothesis for orchestration session:** In `companion_harness/realtime_orchestrator.py`'s `_policy_gate_task`, the gating coroutine may be invoked twice per signal arrival when both the EOU-pipeline path and the addressing-pipeline path each call `decide()` independently. Or `_pending_decision_future` deduplication is not actually deduplicating in the post-#149 wiring path. Reproduce by feeding any signal stream that triggers EOU and grepping the resulting `decision_traces/` for identical `timestamp_mono_ms` across different `seq_no`.
- **Sessions / blobs:**
    - `/tmp/manual_test_blobs/decision_traces/b6f22b26-1f4c-4609-9d47-1ed5ff3957b7-sro-{6,11,16,21,26,32,37,42}-*.json`
    - `/tmp/manual_test_blobs/decision_traces/e4729fa9-e81b-4e46-9af3-c1bb8036991c-sro-{6,11,17,23,29}-*.json` — second session, single-dispatch (no identical-ts pairs observed) but still high rate.

---

## Finding 10 — Audio_out drops ≈85% of `full_response` decisions

- **Scenario:** B (address-the-agent); operator reported "audio output delayed or disappear" during the burst.
- **What I saw:** Mid-round `/healthz`:
    - `audio_out_chunks_sent: 2`
    - Decision-trace count this round: ~13 `full_response` rows across the two sessions
    - Ratio: 2/13 ≈ 15% of `full_response` decisions actually produced audio chunks reaching the browser.
- **What I expected:** Each `full_response` SpeakDecision should produce at least one audio chunk on `/ws/audio_out`. The voice-back loop was claimed end-to-end-wired by PRs #127/#132/#135.
- **Severity:** **bug — operator-visible.** This is the proximate cause of the "delayed/disappearing audio" symptom. The decision lane fires fast; the audio lane falls behind, then most decisions are abandoned.
- **Known issue ref:** new.
- **Hypothesis for orchestration session:** Most likely a downstream consequence of Finding 9 — when the orchestrator emits a second `full_response` for the same turn boundary within ~1 ms, the in-flight Kokoro synthesis for the first decision is cancelled (barge-in semantics applied to the wrong actor: the agent cancelling its own speech). A secondary candidate: the `WebSocketAudioSink` queue is dropping on backpressure without surfacing `log_drop_or_degrade` — but the watcher saw no drop events, so this is less likely. Worth instrumenting `KokoroTtsAdapter` and `WebSocketAudioSink` with explicit `synthesis_started` / `synthesis_cancelled` / `audio_chunk_emitted` events so the ratio can be reasoned about from the event log instead of from a sampled counter on `/healthz`.

---

## Finding 11 — Session leak on page reload

- **Scenario:** B, after operator reloaded the browser page mid-session
- **What I saw:** `/healthz` reports `active_sessions: 2`, `sessions_opened: 2`. Operator reloaded the page once — the prior ingest session is still considered active, not torn down on WS close.
- **What I expected:** `active_sessions` should decrement when an ingest WS closes (browser reload or navigation).
- **Severity:** bug (low) — accumulates state per reload; if the prior session was still consuming GPU/CPU on the orchestrator's coroutines, several reloads during a long debug session could compound load and feed back into Finding 9's high-rate dispatch.
- **Known issue ref:** new.
- **Hypothesis for orchestration session:** `_handle_ingest_ws` in `manual_test_console/server.py` doesn't decrement `active_sessions` or call `ingest.close_session(session)` in its `finally` block. Audit the WS-close cleanup path.

---

## Open question for the orchestration session

We did NOT capture, from this companion session, the actual **transcript text** for any of the 13 `full_response` decisions — Finding 8 from the prior round (transcript not emitted as an Event) remains unfixed, so the decision-trace JSON still doesn't carry `user_transcript`. Without it, we cannot tell whether whisper-tiny is hallucinating on noise/echo (driving repeat dispatches) or whether the operator's actual utterances were being correctly transcribed. Resolving Finding 8 would let a future round confirm/refute the echo-loop hypothesis quantitatively.

---

## Round-2 session summary

- **Closed this round:** Findings 2, 3, 5, 6 (all confirmed fixed by PRs #131, #134, #133, #149).
- **New:** Findings 9 (orchestrator duplicate-dispatch), 10 (audio_out drops ≈85% of `full_response`), 11 (session leak on page reload).
- **Still open from prior round:** Finding 8 (whisper transcript never emitted as Event — also blocks deeper diagnosis of Findings 9/10).
- **Recommended order for the orchestration session:**
    1. **Finding 9** — root-cause the duplicate dispatch. This is upstream of Finding 10 and is also a soft invariant-#5 concern. Most leverage.
    2. **Finding 10** — once Finding 9 is fixed, re-measure `audio_out_chunks_sent` vs `policy_decision` count. If still dropping, instrument TTS+sink as suggested.
    3. **Finding 8** — emit `asr_transcript_emitted` events so the next round can diagnose hallucination quantitatively.
    4. **Finding 11** — quick fix in the ingest-WS close handler.
    5. **Handbook hygiene** — the §0 status table, scenario B/D/E markers, and §4 troubleshooting row for "every utterance comes back as silence" all still treat Finding 6 as open even though the top-of-file headline box says CLOSED. Reconcile in one pass.
- **Live evidence preserved:** `/tmp/manual_test_blobs/decision_traces/` (all rounds combined). Server still running at writeup time; `/healthz` returns `live_pipeline_enabled: true`. Live watcher is still streaming events via `/tmp/manual_test_watcher.sh` (Monitor task `bhnjqxcbk`) — stop with TaskStop or by killing the script if no further testing is planned.

---

*Round 2 findings written by the test-companion session 2026-05-15 evening. Live watcher script: `/tmp/manual_test_watcher.sh`. Decision-trace evidence: `/tmp/manual_test_blobs/decision_traces/*.json` on b200 (this host).*

---

# Manual-test findings — 2026-05-15 (Round 3, late evening)

Round 3 ran after the orchestration session reported "bugs fixed" for Findings 9, 10, 11 and restarted the server (new PID `353198`, started from worktree `/tmp/wt-console-live`). Banner gained five additional real adapters: `scene_scorer:real:CLIPSceneChangeScorer`, `grounding_model:real:GroundingDINOAdapter`, `av_conflict_scorer:real:HeuristicAVConflictScorer`, `urgency_scorer:real:ProsodyLexiconUrgencyScorer`, `embedder:real:SentenceTransformerEmbedder` (only `deictic_model:stub:_NullDeicticModel` remained stub). `--enable-vision` was now ON, so Phase 2 scenarios were reachable too.

Blob store was wiped at restart (clean slate). One session in this round: `e61c1ad0`.

---

## Finding 9 — NOT FIXED. Reproduced in round 3 after fewer than 40 seconds.

- **Scenario:** B (address-the-agent), repeated turns.
- **What I saw, full session (8 full_response decisions in 37s):**
    | sro | ts (mono ms) | gap | note |
    |---|---:|---:|---|
    | 7  | 700999343 | — | single |
    | 13 | 701008571 | +9228 | single |
    | 20 | 701011799 | +3228 | single |
    | 26 | 701015030 | +3231 | single |
    | 32 | 701019636 | +4606 | **paired** |
    | 37 | 701019636 | **+0** | **paired** (identical ts, different seq) |
    | 46 | 701030105 | +10469 | single |
    | 54 | 701036902 | +6797 | single |
  Every decision: `action=full_response / reason=EOU_CONFIRMED / supporting=USER_ADDRESSED_AGENT`.
- **What changed vs round 2:** The fix may have cleaned up the most-aggressive triple-dispatch pattern (round 2 saw triples at sro-32/37/42; round 3 only a pair). But the duplicate-dispatch path is still reachable, just less consistently. **The bug is reachable, not eliminated.**
- **Severity:** **bug — re-open.** Suggested for orchestration session: the dedup is racy, not absent. Whatever sentinel `_pending_decision_future` checks in `_policy_gate_task`, it's evaluable as False at moments when a second signal slips in. Concrete repro: keep speaking with wake-word; eventually two signals will land within the same coroutine tick and both reach `decide()`. Recommend adding an explicit `decision_dispatched_at_ms` per-turn-boundary guard inside the orchestrator, plus a contract test that hits the orchestrator with two `smart_turn_signal` events ≤ 5 ms apart for the same logical turn and asserts exactly one `policy_decision` emitted.

## Finding 10 — partially improved but still leaky

- **What I saw:** At sample point with 2 full_response decisions on disk, `audio_out_chunks_sent=1`. At sample with 4 decisions, `audio_out_chunks_sent=2`. Ratio ~50% — better than round 2's 15%, but not 1:1.
- **Caveat:** `audio_out_chunks_sent` counts WebSocket sends, and Kokoro may stream multiple chunks per synthesis (so 1:1 isn't necessarily the expected ratio). Without instrumentation to separate "synthesis started but produced 0 chunks" from "synthesis started and produced N>1 chunks", we can't tell from /healthz alone whether decisions are still being dropped or whether the chunking is just lumpier than expected.
- **Severity:** likely-bug, evidence-incomplete. Reiterating the round-2 suggestion: instrument `KokoroTtsAdapter` and `WebSocketAudioSink` with explicit `synthesis_started` / `synthesis_cancelled` / `synthesis_completed` / `audio_chunk_emitted` events so the chunks-per-decision distribution is visible in the event log. Until then, the operator's user-facing experience (Finding 12 below) is the only honest indicator.

## Finding 11 — appears fixed

- **What I saw:** Operator reloaded the page mid-session; `/healthz` reported `active_sessions=1, sessions_opened=1` after the reload. (Worth retesting with multiple reloads to confirm the leak doesn't accumulate, but the single-reload case is clean.) ✓

## Finding 12 — **NEW headline.** System hallucinates outputs and does not respond to actual speech

- **Scenario:** B (address-the-agent), throughout round 3
- **What the operator reported:** "It still didn't respond to my words but output hallucinations." Specifically: spoken wake-word + question got no relevant response; meanwhile, the system fired `full_response` decisions during silent stretches and generated speech that bore no relationship to what was said.
- **What I saw on the event side:** 8 `full_response` decisions in 37 seconds, every single one `action=full_response / reason=EOU_CONFIRMED / supporting=USER_ADDRESSED_AGENT / path=…/user_addressed_agent:full_response`. The decisions fire whether or not the operator spoke. The operator's actual utterances either don't produce a meaningful response or get drowned out by hallucination-driven responses that play first.
- **What I expected:** Per handbook §2.5 scenario B, the user's question should produce a `full_response` whose synthesized audio is a coherent reply to the question; silent stretches and noise should NOT produce `full_response` (silence wins ties — invariant #8). Today the gates the spec relies on to enforce that are not actually discriminating.
- **Severity:** **bug — load-bearing.** This is the round-3 equivalent of round 2's headline. Different proximate cause (post-PR-#149 the addressing gate is wired, but the new tier 3 — solo-fallback "user_addressed_agent=True when only one human is present" — defeats the protection in exactly the configuration the rig is run in, namely a single operator alone). Combined with whisper-tiny's well-documented tendency to hallucinate short phrases on silence/noise (e.g., "you", "Thanks for watching", "Bye"), every spurious EOU + transcript-of-noise becomes addressed-to-agent → full_response → TTS speaks. The user-visible result is a system that talks to itself and ignores its operator.
- **Known issue ref:** new (relates to but does not duplicate previous Findings 6, 7).
- **Hypothesis for orchestration session:**
    1. The 3-tier AddressingClassifier (PR #149) has the right *primary* (wake-word) and the right *secondary* (diarization social_mode) but a wrong *fallback* (solo → always addressed). In a single-operator manual-test rig, every transcript — including hallucinations — reaches tier 3. The fix is to make tier 3 either (a) require a non-trivial transcript length / token count, (b) require the transcript to *not* match a whisper-hallucination denylist (the most common short phrases produced from silence), or (c) default to False when transcript confidence/log-prob is below a threshold. Silence-wins-ties (invariant #8) suggests (c) is the spec-consistent choice.
    2. Independently, Smart Turn v3 is producing EOU on silence/breath that whisper then transcribes into hallucination. The Smart Turn `silence_rms_threshold` and `silence_onset_ms` are tunable via the threshold dashboard (PR #153); a follow-up tuning pass with the dashboard might reduce the upstream false-EOU rate.
- **Why we can't quantify hallucination directly:** Finding 8 (transcript not emitted as Event, not stored in `decision_trace`) is still open. The decision_trace JSON still has only `{config_version, counterfactuals, decision_id, input_event_ids, model_adapter_versions, policy_version, primary_reason_code, redacted_explanation, retrieval_used, sensitive_explanation_ref, signal_event_ids, supporting_reason_codes, threshold_path}` — no `user_transcript`. Until Finding 8 is fixed, "whisper hallucinated X" remains an inference from operator UX, not a fact extractable from the audit trail. **Resolving Finding 8 is now blocking Finding 12 diagnosis.** Highest leverage of the open findings.
- **Sessions / blobs:** `/tmp/manual_test_blobs/decision_traces/e61c1ad0-e9f6-4e1d-bd40-2436d50c2c18-sro-{7,13,20,26,32,37,46,54}-*.json` on b200.

---

## Round-3 session summary

- **Verified fixed:** Finding 11 (session leak — single reload now clean). Others not verifiable from this round.
- **Re-opened:** Finding 9 (orchestrator duplicate dispatch — fix is incomplete; reaches a duplicate within ~40 s of testing).
- **Partially improved, evidence-incomplete:** Finding 10 (audio_out chunks vs decisions improved from 15 % to ~50 %, but full-loop verification needs explicit synthesis-lifecycle events).
- **New headline:** Finding 12 — system speaks hallucinations and doesn't respond to operator. Proximate cause: post-#149 AddressingClassifier's tier-3 solo-fallback turns every transcribed snippet (including whisper-tiny hallucinations on silence/noise) into `user_addressed_agent=True`. Spec-consistent fix is to make tier 3 default False (silence wins ties, invariant #8) and require positive evidence to flip True.
- **Still blocking:** Finding 8 (transcript not on event bus). Resolving this unblocks quantitative diagnosis of Finding 12 and any future ASR/intent/retrieval debugging.
- **Recommended order for orchestration session:**
    1. **Finding 8** — emit `asr_transcript_emitted` events. Cheap, structural, and necessary for everything else.
    2. **Finding 12** — make solo-fallback default False (or transcript-quality-gated). The simplest single-line fix that should resolve the operator-facing "ignored + hallucinating" symptom.
    3. **Finding 9** — re-investigate the duplicate-dispatch race. Add the contract test described above.
    4. **Finding 10** — instrument TTS lifecycle events; re-measure after Finding 9 is solid.
- **Watcher status at writeup:** still running (Monitor task `bhnjqxcbk`, script `/tmp/manual_test_watcher.sh` on b200 PID `126625`). Stop with TaskStop if no further testing is planned.
- **Live evidence preserved:** `/tmp/manual_test_blobs/decision_traces/` (8 traces from session `e61c1ad0`). Server still running. `/healthz` returns `live_pipeline_enabled: true`.

---

*Round 3 findings written by the test-companion session 2026-05-15 late evening. Live watcher script: `/tmp/manual_test_watcher.sh`. Decision-trace evidence: `/tmp/manual_test_blobs/decision_traces/*.json` on b200 (this host).*

---

# Manual-test findings — 2026-05-15 (Round 4, very late evening)

Round 4 followed the round-3 writeup and another orchestration-session pass on Findings 9/10/11. Server kept running (same PID `353198`, same banner: all real adapters wired, `deictic_model` still stub, `--enable-vision` ON).

Three sessions exercised in this round across page reloads: `e61c1ad0` (residual from round 3), `191b04fe`, `a215abde` (longest, ~200+ events). Decision-trace store carries cumulative evidence from all three.

---

## Verified outcomes

| Finding | Round-4 verdict | Evidence |
|---|---|---|
| **10** — audio_out drops `full_response` decisions | ✅ **FIXED** (numerically and end-to-end) | Final session `a215abde`: 9 `full_response` decisions, `audio_out_chunks_sent=19` (≈2.1 chunks/decision — Kokoro is streaming, ratio is healthy). Operator confirmed "I can hear the audio" — playback works end-to-end. |
| **11** — session leak on page reload | ✅ **HOLDS** | Across 3 reloads in this round, `active_sessions` settled at 1 (the live page) — `sessions_opened=3, active_sessions=1`. No leak. |
| **9** — orchestrator duplicate-dispatch | ❌ **REOPEN — WORSE** | See Finding 9 below: round-4 hit a **quadruple** backchannel dispatch (4 decisions in 1 ms), plus two triples — escalation versus round-3's pair. Bug is generalized beyond `full_response` to backchannel action type too. |
| **12** — system hallucinates outputs, ignores operator | ❌ **CONFIRMED END-TO-END** | Operator paraphrase: "I can hear the audio, but the content had no relation to my words and were strongly delayed." Hallucinated audio plays back to the operator at high latency; real utterances either get ignored or get queued behind hallucination-driven responses. |
| **13** — SmartTurn doesn't suppress thinking pauses | ❌ **CONFIRMED** | Operator: "after I said 'I think' and stopped, it still displayed `policy_decision → full_response / EOU_CONFIRMED` rather than recognize that I had not finished talking." Handbook scenario D claim that PR #126's Pipecat Smart Turn v3 suppresses mid-pause EOU is refuted in vivo. |
| **8** — transcript not on event bus | ❌ **STILL OPEN, NOW BLOCKING** | Decision-trace JSON unchanged: still no `user_transcript`. This is the structural reason Findings 12, 13 cannot be quantified from the audit trail. |

---

## Finding 9 (round 4) — duplicate-dispatch ESCALATED, generalized to backchannel

In session `a215abde` between ts `701444416` and `701445824` (a ~1.4-second stretch), the orchestrator produced eleven `backchannel` decisions in three near-instant clusters:

| burst @ ts (mono ms) | seqs | count |
|---|---|---:|
| 701444416 / 416 / 416 / 417 | 24, 30, 36, 42 | **4** |
| 701444651 / 652 / 652 | 49, 55, 61 | 3 |
| 701445544 / 545 / 545 | 69, 75, 81 | 3 |
| 701445824 | 89 | 1 |

That is **escalation versus round 3** (pair → quadruple) and **generalization** (the bug fires on `action=backchannel`, not just `action=full_response`). Every burst row carries `reason=BACKCHANNEL_DETECTED / threshold_path=eou_gate:passed/backchannel_threshold:exceeded`. Outside the burst window, dispatches in this session are single (~30 of 31 events post-storm are clean singles, mixing backchannel and full_response).

**This rules out the "race condition unique to full_response gating" hypothesis from round 3.** The race is in the orchestrator's decision-dispatch path itself, fired by any decision-eligible signal, and amplified specifically when consecutive matching signals arrive in tight temporal clusters (e.g., a flurry of backchannel-tier-exceeded frames).

- **Severity:** **bug — load-bearing.**
- **Hypothesis for orchestration session:** the `_pending_decision_future` guard in `_policy_gate_task` (`companion_harness/realtime_orchestrator.py`) is checked-then-set without atomicity. Under asyncio, that's fine for sequential signal arrival, but if multiple signals are awaiting on the same scheduling tick and the gate coroutine yields between check and set, several of them all observe `_pending_decision_future is None` and each spawn their own decide() coroutine. The reproducer: feed N `backchannel_classification` events in the same loop iteration (e.g., one whisper-tiny pass that produces N high-confidence frames in a buffered batch) and observe N decisions in the trace store.
- **Suggested fix shape:** wrap the check-and-set in a single non-yielding sequence (asyncio.Lock or a synchronous `if ... else: return`), AND add a contract test that drives the orchestrator with three `backchannel_classification` signals within the same asyncio tick and asserts exactly one `policy_decision`.
- **Sessions / blobs:** `/tmp/manual_test_blobs/decision_traces/a215abde-b2b4-4d53-a6c5-a936d2779bce-sro-{24,30,36,42,49,55,61,69,75,81,89}-*.json`.

---

## Finding 12 (round 4) — confirmed end-to-end, **now blocks all other diagnosis**

- **Scenario:** B (address-the-agent), and any other scenario, all rounds.
- **Operator's exact words this round:** "I can hear the audio, but the content had no relation to my words and were strongly delayed." Earlier in the round: "It still didn't respond to my words but output hallucinations." Later: "still hallucinations so I can't tell whether Smart-turn silence onset made effect."
- **What I saw on the event side:** All `full_response` decisions across rounds 3 and 4 still carry the identical signature — `reason=EOU_CONFIRMED / supporting=USER_ADDRESSED_AGENT / path=…/user_addressed_agent:full_response`. With Finding 8 unresolved (transcript not in trace), we still cannot show *what whisper-tiny transcribed* to drive each decision.
- **Severity:** **bug — load-bearing AND meta-blocker.** Beyond making the rig unusable end-to-end (system speaks irrelevant content, ignores operator), Finding 12 also actively prevents diagnosis of every other open finding: the operator cannot tell whether SmartTurn correctly held during a pause (Finding 13), whether their wake-word was recognized, whether the response latency is the queue from Finding 14 or a different bottleneck, etc. **No future round will extract useful diagnostic signal until Finding 12 is fixed.**
- **Reiterated root-cause hypothesis (from round 3):** AddressingClassifier's tier-3 solo-fallback ("user_addressed_agent=True when only one human present") defeats the addressing gate in the manual-test rig's exact configuration. Spec-consistent fix: tier 3 defaults to False, requires positive evidence (non-trivial transcript length, transcript not on whisper-hallucination denylist, transcript confidence above threshold) to flip True. Silence wins ties (invariant #8).

---

## Finding 13 — SmartTurn v3 does not suppress mid-utterance thinking pauses

- **Scenario:** D (thinking pause), per handbook §2.5.
- **What I saw:** Operator said "I think" then paused. Per handbook §2.5 scenario D, Smart Turn v3 (real, PR #126) is supposed to suppress the mid-pause EOU — `smart_turn_signal` with `p_done < 0.5` should fire on the gap, no `full_response` until continuation. Instead, `policy_decision → full_response / EOU_CONFIRMED` fired immediately on the pause. The thinking pause was misclassified as end-of-utterance.
- **What I expected:** Per handbook, Smart Turn v3 observably holds across a 1.5 s in-utterance pause. Per banner, `smart_turn_model: "Pipecat SmartTurn v3 (ONNX, CPU)"` is loaded with `use_stubs=false`.
- **Severity:** bug. Either (a) Smart Turn v3 is loaded but not actually wired into the EOU gate (despite the banner), (b) `detectors.smart_turn.silence_onset_ms` default is too short and crosses normal speech pauses, or (c) the EOU gate is taking `max(p_done)` across detectors and Silero VAD's `p_done` is dominating Smart Turn's suppression.
- **Known issue ref:** new. Note this contradicts the handbook's "✅ Validates" status row for SmartTurn — that row needs updating once root cause is known.
- **Cannot fully isolate via the threshold dashboard right now because of Finding 12** — even if raising `silence_onset_ms` works, the operator can't tell because the system is busy speaking hallucinated content. Re-run scenario D after Finding 12 is fixed.

---

## Finding 14 — Strong end-to-end response latency (symptomatic of Finding 12)

- **Scenario:** B (address-the-agent).
- **Operator's words:** "[audio responses] were strongly delayed."
- **What I saw on the event side:** When the orchestrator fires `full_response` on hallucinations during silent stretches, those decisions queue MiniCPM-o → Kokoro synthesis work. When the operator's actual utterance arrives, its turn lands at the tail of the queue. The operator hears the response to their real words long after they spoke, with N hallucinated responses played first.
- **Severity:** bug (symptomatic). Likely collapses automatically once Finding 12 is fixed (no more hallucination-driven queue depth). Worth re-measuring after Finding 12; only escalate to its own root-cause investigation if latency persists when the queue depth is small.
- **Known issue ref:** new.

---

## Round-4 session summary

- **Verified fixed:** Findings 10 (audio_out), 11 (session leak).
- **Reopened, worse than round 3:** Finding 9 (orchestrator duplicate-dispatch — now quadruple, and generalized to backchannel action type, not just full_response).
- **Confirmed end-to-end:** Findings 12 (hallucinate-and-ignore), 13 (SmartTurn doesn't suppress thinking pause).
- **New:** Finding 14 (E2E latency — symptomatic of 12).
- **Still blocking everything:** Finding 8 (transcript not on event bus). Resolving it unblocks quantitative diagnosis of every other open finding.
- **Meta-finding:** Finding 12 also blocks diagnosis of 13 and any audio-side diagnosis; no further test round will extract useful signal until 12 is fixed.

**Recommended order for the orchestration session, replacing the round-3 order:**

1. **Finding 12 first** (the meta-blocker). Default the AddressingClassifier tier-3 solo-fallback to False, gate it on transcript quality / confidence / wake-word presence. Spec-consistent (silence wins ties — invariant #8). Single-line conceptual change; probably a few lines of code in `companion_harness/addressing_classifier.py` + `realtime_orchestrator.py` integration. Single highest-leverage fix.
2. **Finding 8** (structural, unblocks diagnosis forever). Emit `asr_transcript_emitted` Event after the EOU transcribe call in `realtime_orchestrator.py` line 400-405; include the transcript in the `decision_trace` JSON. After this, future rounds can show "whisper hallucinated 'thanks for watching'" directly from the audit trail instead of inferring from operator UX.
3. **Finding 9** (correctness, race in dispatch path). Wrap `_pending_decision_future` check-and-set in a non-yielding sequence; add contract test that drives N coincident `backchannel_classification` signals and asserts exactly one `policy_decision`.
4. **Finding 13** (after 12 is fixed so the rig is usable again). Re-test scenario D with default thresholds, then tune via dashboard if needed. May reveal a deeper wiring bug if dashboard tuning has no effect.
5. **Finding 14** — re-measure after 12. Likely collapses on its own.
- **Handbook drift to reconcile** (low priority): §0 status table and scenarios B/D/E/I-K still carry "⚠️ Finding-6 affected" markers even though the top-of-file headline says Finding 6 is CLOSED. Update the table and scenario markers in one pass once the recommended order's fixes land.

**Watcher status:** stopped (Monitor task `bhnjqxcbk` ended via TaskStop; standalone `/tmp/manual_test_watcher.sh` process killed).

**Evidence preserved:** `/tmp/manual_test_blobs/decision_traces/` — combined evidence from rounds 1-4 (operator has not requested cleanup; do not `rm` before triage). Server still running (PID `353198`, healthz green); kill via `kill 353198` only if a redeploy is needed.

---

*Round 4 findings written by the test-companion session 2026-05-15 very late evening. Combined session log spans 4 rounds across the day. Watcher stopped. Decision-trace evidence: `/tmp/manual_test_blobs/decision_traces/*.json` on b200 (this host).*

---

# Manual-test findings — 2026-05-15 (Round 5, around midnight)

Round 5 ran after the orchestration session pushed fixes for Findings 8 and 12 (suggested as the highest-leverage pair at the end of round 4). Server restarted at 23:11 — new PID `469216`, same banner (all real adapters wired, `deictic_model` still stub, `--enable-vision` ON, `use_stubs=false`).

One main session this round: `30e6b8bf` (preceded by a brief warm-up session `baa9e26e` that produced 7 traces).

The decision-trace JSON now carries new fields `user_transcript` and `user_transcript_preview` — Finding 8 fix is observable. The watcher was upgraded to surface these inline, so every trace notification now reads e.g. `act=silence reason=NOT_ADDRESSED_TO_AGENT … tx='Hello?'`. That visibility made several of round-5's findings reachable from this session.

---

## Verified outcomes — major progress

| Finding | Round-5 verdict | Evidence |
|---|---|---|
| **8** — transcript not on event bus | ✅ **FIXED** | `decision_trace.json` now includes `user_transcript` + `user_transcript_preview`. Watcher can show inline. |
| **12** — system hallucinates outputs, ignores operator | ✅ **MOSTLY FIXED** at the addressing gate. Whisper hallucinations like `'Hello?'`, `'Huh?'`, `'<none>'` now route to `silence/NOT_ADDRESSED_TO_AGENT` instead of `full_response`. **But a new symptom (Finding 17) reveals the bigger problem moved one layer downstream — see below.** |
| **9** — orchestrator duplicate-dispatch | ⚠️ **NOT DIRECTLY OBSERVED THIS ROUND** but no identical-`timestamp_mono_ms` pairs in the 23 decisions of session `30e6b8bf`. Cannot claim fixed — round-4 fix was incomplete and would need a contract test to verify. |
| **10** — audio_out drops | ✅ **HOLDS** | 10 audio chunks for 8 `full_response` decisions (~1.25/decision; Kokoro streams). |
| **11** — session leak | ✅ **HOLDS** | `active_sessions=0` after operator closed page; no leak. |
| **13** — SmartTurn doesn't suppress mid-utterance pauses | ❌ **CONFIRMED IN NEW FORM** | See Finding 13 update below — fragments continuous real speech into multiple decisions. |

Action distribution in `30e6b8bf` (23 decisions): **11 silence (48%), 8 full_response (35%), 4 backchannel (17%)** — healthy mix, dramatically better than round 4's near-100 % `full_response`+`backchannel`. The Finding 12 fix is doing real work.

---

## Finding 13 (round-5 update) — SmartTurn fragments continuous speech, not just thinking pauses

In a single continuous spoken phrase by the operator (likely "I should put up some fight now" or similar), the orchestrator emitted two separate `full_response` decisions:

- `tx='I should put' → full_response / EOU_CONFIRMED / USER_ADDRESSED_AGENT`
- `tx='some fight now.' → full_response / EOU_CONFIRMED / USER_ADDRESSED_AGENT`

Same continuous utterance, split mid-sentence. So Finding 13 is broader than "doesn't suppress thinking pauses" — SmartTurn is firing EOU mid-real-utterance whenever the operator naturally breathes/pauses for a sub-second between words. Each fragment then triggers a separate response.

Earlier `tx='I think.' → silence (silence:fallthrough)` from this same session looked OK only because the addressing classifier downstream rejected it. EOU is still misfiring on the pause — the fact that the operator-visible behavior is now "silence" is incidental to Finding 12's fix masking Finding 13.

**Severity:** bug. Recommended: re-test scenario D specifically with the wake-word ("Claude, I think… …maybe later") to deliberately expose the EOU misfire when addressing returns True. Then either retune `detectors.smart_turn.silence_onset_ms` via the dashboard (PR #153) or check whether Smart Turn v3 is actually gating EOU or just being logged alongside it.

---

## Finding 15 (new) — `backchannel` decision fires on empty transcript

- **What I saw:** Two consecutive `act=backchannel reason=BACKCHANNEL_DETECTED tx='<none>'` decisions in session `30e6b8bf`, interleaved with `act=silence tx='<none>'` decisions. Same empty-transcript input produces non-deterministic actions.
- **What I expected:** A `backchannel` decision should require an actual vocal acknowledgement from the user, which by definition produces some transcript content. An empty transcript should route to `silence`.
- **Severity:** bug — also a soft invariant-#5 (replay determinism) concern: identical-looking input producing different output.
- **Hypothesis:** the backchannel classifier (per banner: `whisper-tiny + lexicon`) is making its decision from audio-energy features without requiring `len(transcript) > 0` as a precondition. Add the precondition; if the audio classifier still wants to fire, log it but route to `silence`.

## Finding 16 (new) — Whisper-tiny silence-hallucination phrases route to `backchannel`, not `silence`

- **What I saw:** Two decisions:
    - `tx='Thanks for watching.' → backchannel`
    - `tx='Okay, okay.' → backchannel`
  Both are textbook whisper-tiny silence hallucinations ("Thanks for watching" is *the* canonical YouTube-derived hallucination; "Okay, okay" is in the same family). Meanwhile `tx='Huh?' → silence` was correctly suppressed. The threshold between "hallucination → silence" and "hallucination → backchannel" is not consistent.
- **What I expected:** Known-hallucination phrases should be on a denylist that forces `silence`, not run through the backchannel classifier.
- **Severity:** bug. Lower-severity than Finding 17 in operator impact but it pollutes conversational state — the agent's social-mode tracker will mark the operator as having "acknowledged" things they never said.
- **Suggested fix:** add a hallucination denylist alongside the wake-word list. Phrases like "Thanks for watching", "Bye", "you", "Okay, okay", "Hello?" — whisper-tiny's documented silence outputs — should force `silence/NOT_ADDRESSED_TO_AGENT` regardless of the backchannel classifier's score. See e.g. published lists of whisper-hallucination phrases for English models.

## Finding 17 (new) — **NEW HEADLINE.** Response content does not match transcribed user question

- **Operator paraphrase:** "but the response doesn't match my words." Heard repeatedly across multiple `full_response` decisions whose transcripts were correctly captured.
- **What I saw on the trace side:** Several clean question-shaped transcripts producing `full_response`:
    - `tx="So, what's the weather today?"`
    - `tx="What's the weather today?"`
    - `tx='What can you do for me?'`
    - `tx="What's your model?"`
  All four correctly routed to `full_response / EOU_CONFIRMED / USER_ADDRESSED_AGENT`. Audio chunks were sent (`audio_out_chunks_sent=10` against 8 `full_response`s). But the operator reports the spoken responses didn't address these questions.
- **What it means:** the bug from Finding 12 has not been fully fixed — it has moved one layer downstream. Round 4: addressing was always True → hallucinations approved. Round 5: addressing correctly rejects hallucinations → real questions approved → but the *response content* generated by MiniCPM-o (or pulled from a stale proposal queue) doesn't track the just-transcribed question.
- **Severity:** **bug — load-bearing replacement headline.** The agent now correctly listens but speaks unrelated content. From the operator's point of view this is still "the agent doesn't answer my question," even though the audit trail shows perfectly correct gating decisions.
- **Hypothesis for orchestration session — four candidates, ordered by likelihood:**
    1. **Stale-proposal pickup.** MiniCPM-o generates proposals speculatively during silent stretches. When a real `full_response` fires, the orchestrator may be pulling the next available proposal off a shared queue instead of the proposal specifically generated for *this turn's* transcript. Result: operator's "weather" question gets answered with a proposal originally generated for some earlier hallucination input.
    2. **`user_transcript` not threaded into the proposal call.** The proposal-generation invocation may not actually be receiving the just-computed `inputs.user_transcript` and is therefore generating from prior context only. The transcript made it into the decision-trace but not into the foreground model's input.
    3. **TTS reorder.** Audio synthesis from an earlier turn could still be in flight when a newer turn approves; if the WebSocketAudioSink doesn't preempt-and-replace on new `full_response`, the operator hears responses in the wrong order relative to their questions.
    4. **MiniCPM-o context confusion.** The model may be receiving the right transcript but its long-context state is dominated by earlier turns, so its response anchors to old content.
- **How to triage from b200 side without further operator testing:** instrument the foreground-model invocation in `companion_harness/realtime_orchestrator.py` (around the proposal-generation call) with two new event types — `foreground_proposal_requested(decision_id, user_transcript)` and `foreground_proposal_returned(decision_id, proposal_text_hash)` — both carrying the `decision_id` and (for the request) the transcript actually passed in. Then for the next round, the watcher can verify decision-by-decision that the proposal text was generated for the right transcript. This is essentially the Finding-8 instrumentation extended one layer further.

---

## Round-5 session summary

- **Major progress:** Findings 8, 10, 11, 12 (the round-4 meta-blocker) all verified fixed or holding. The decision-trace store now carries transcripts, and hallucinations stop at the addressing gate instead of triggering responses. This was a successful fix-pass.
- **Still present:** Finding 13 (SmartTurn fragments continuous speech as well as thinking pauses; was masked by Finding-12 fix this round).
- **Not directly observed but unresolved:** Finding 9 (no identical-`ts` clusters in this round's 23 decisions, but round-4 fix was admittedly incomplete; not safe to claim fixed without the contract test).
- **New:** Findings 15 (backchannel on empty transcript), 16 (whisper hallucinations route to backchannel), 17 (response content doesn't match transcribed question).
- **New headline:** Finding 17. The operator-visible "agent doesn't answer my question" symptom has migrated from addressing-gate failure (Finding 12) to proposal/dispatch-pipeline failure (Finding 17). End-to-end usability is still blocked.

**Recommended order for the orchestration session, replacing the round-4 order:**

1. **Finding 17** — the new meta-blocker. Until response content tracks the transcribed question, all downstream UX testing is meaningless. Highest-leverage triage: add `foreground_proposal_requested/returned` events with `decision_id` + transcript-hash so the next round can verify the proposal was generated for the right input. Then root-cause (stale-queue vs context-not-threaded vs TTS-reorder).
2. **Finding 13** — re-test scenario D with the wake-word ("Claude, I think… maybe later") to expose the EOU misfire when addressing returns True. Then tune `silence_onset_ms` via dashboard or fix Smart Turn wiring.
3. **Finding 16** — add whisper-hallucination denylist (small, well-bounded fix; published lists exist).
4. **Finding 15** — add `len(transcript) > 0` precondition on backchannel classifier.
5. **Finding 9** — add the contract test from round 4 (N coincident `backchannel_classification` events ≤ 5 ms apart → exactly one `policy_decision`); even if the round-4 fix held in round-5 sampling, the bug class needs a test gate.

**Watcher status:** stopped (Monitor task `bpa3bmxef` ended via TaskStop; script `/tmp/manual_test_watcher.sh` killed).

**Evidence preserved:** `/tmp/manual_test_blobs/decision_traces/` — combined evidence from rounds 1-5. Round-5-specific traces are in sessions `baa9e26e-*` (7 warm-up) and `30e6b8bf-*` (23 main). Operator has not requested cleanup; do not `rm` before triage. Server still running (PID `469216`, healthz green).

---

*Round 5 findings written by the test-companion session 2026-05-15 midnight. Largest single-round forward step of the day — Findings 8, 10, 11, 12 closed; new headline Finding 17 isolates the remaining usability blocker to the proposal/dispatch pipeline rather than the addressing gate. Decision-trace evidence: `/tmp/manual_test_blobs/decision_traces/*.json` on b200 (this host).*
