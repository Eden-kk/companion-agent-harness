# Manual-test handbook

Status: **DRAFT** — written alongside Phase 1 service build (2026-05-15). Update the "Exact commands" section once the manual-test service PR lands.

This handbook is what the project lead reads when they want to actually use the harness with their voice (Phase 1: audio-only) and later with their voice + camera (Phase 2: audio + video). It is **not** a substitute for the architecture spec — it is the runbook that complements it.

If you are looking for *what the harness is for*, read `docs/architecture-v0.1.md`. If you are looking for *how to run a session and what to expect*, read this.

---

## 0. Scope and honesty

**Phase 1 — Audio-only manual test.** You speak into your laptop mic. The harness on b200 ingests audio, runs VAD, runs SpeakPolicy, emits SpeakDecisions, and shows you everything on a web console. **It does not talk back.** There is no synthesized voice output in Phase 1; the live-loop integration milestone (`docs/milestone-live-loop-integration-draft.md`) is the milestone that adds that, and it is a separate track.

What you can validate in Phase 1:
- The mic capture path closes the causal graph: `harness_init → session_open → raw_audio → vad_turn_signal → SpeakDecision`.
- VAD fires `vad_turn_signal` events with sensible `p_done` / `p_continue` values as you speak vs. stay silent.
- SpeakPolicy returns `full_response` when you address the agent and `silence` when you don't.
- Privacy gates and memory commits work (if you exercise the memory-bearing flows).
- The EventLogger never blocks the realtime path; backpressure surfaces as `log_drop_or_degrade` events, never silence.

What Phase 1 **does not** validate (because the harness genuinely cannot do it yet):
- Synthesized voice output.
- Barge-in cutting voice mid-utterance.
- Direct-question latency end-to-end (no voice out → no end-to-end timing).
- Anything that requires a closed conversational loop.

**Phase 2 — Audio + video manual test.** You speak and the camera streams to b200 too. The harness ingests `raw_video` events alongside `raw_audio`. The VisionSidecar (`companion_harness/vision_sidecar.py`) sees the frames; the foreground model (MiniCPM-o on b200) can incorporate them. Same observability console; an additional video panel renders frame thumbnails and any vision-related signals.

What Phase 2 adds that Phase 1 lacks:
- Visual context flowing into the foreground model's proposals.
- Deictic-reference detection (Stage 2 capability) becomes exercisable in vivo, not just in fixtures.
- The audio-visual conflict path (`audio_visual_conflict_score > 0.7 → clarification`) becomes reachable.

**Phase 2 still does not validate spoken-back voice.** That remains gated on the live-loop integration milestone.

---

## 1. Prerequisites

- **A working b200 SSH connection.** `ssh b200` should succeed without a password prompt for everyday use. Verify on first use; see `docs/remote-dev.md`.
- **A laptop with a working mic** (and webcam for Phase 2). Browser permissions for mic/camera will be requested on first open.
- **Chrome, Firefox, or Safari (recent).** The console uses `getUserMedia` + `WebSocket`; no fancy features required.
- **The canonical venv on b200** at `/raid/yid042/venvs/companion-harness/` with all `requirements.txt` deps installed. Confirm with `/raid/yid042/venvs/companion-harness/bin/python3 -c "import websockets, aiohttp"`.
- **The repo on b200** at `~/companion-agent-harness/` at a commit that contains the manual-test service. After the Phase 1 PR merges, `git pull origin main` on b200 is enough.

If any prerequisite is not in place, fix it before continuing. Don't paper over it.

---

## 2. Phase 1 — Audio-only walkthrough

### 2.1 Start the server on b200

Open one terminal and SSH to b200:

```sh
ssh b200
cd ~/companion-agent-harness
git pull origin main
/raid/yid042/venvs/companion-harness/bin/python3 -m manual_test_console.server \
    --host 0.0.0.0 --port 8800
```

> The exact module path and flags are pinned by the Phase 1 PR; if they differ, update this handbook.

The server should print a startup banner with:
- The port it bound to (`8800`).
- The blob store path (e.g., `/tmp/manual_test_blobs/<session_id>/`).
- The canonical venv python path.
- EventLogger drain status ("started" — if it says "blocked" or "not started", stop and investigate).

Leave this terminal open. The server stays in the foreground.

### 2.2 Forward the port to your laptop

In a second local terminal:

```sh
ssh -L 8800:localhost:8800 b200
```

This forwards local `:8800` → b200's `:8800`. Keep this shell open too. (Cloudflare quick tunnel is the alternative if `ssh -L` is inconvenient — see `docs/manual-test-module-plan-draft.md` §6.)

### 2.3 Open the console

In your browser: `http://localhost:8800/`.

You should see the manual-test console page. Click **Grant mic** (or the equivalent) when prompted. The page should immediately start streaming PCM16 audio over the WebSocket and showing rows in the event panel.

### 2.4 What you should see

The page renders four lanes (or panels — exact UI is fixed by the Phase 1 PR):

1. **Ingested input events** — `raw_audio_chunk` rows with `seq_no`, `payload_hash`, and `caused_by[]`. New rows arrive ~10×/second.
2. **VAD TurnSignals** — `vad_frame` rows and the occasional `vad_turn_signal` with `p_done`, `p_continue`, `confidence`. `p_done` rises when you stop talking; `p_continue` rises when you're mid-utterance.
3. **SpeakPolicy decisions** — `SpeakDecision` rows with `action_type` (`silence` / `full_response` / `backchannel` / etc.), `primary_reason_code`, and `caused_by[]`.
4. **Causal graph trace** — the DAG that closes each utterance. Orphan count must stay at zero. If it climbs, that's an invariant #1 violation worth reporting.

### 2.5 Scripted scenarios to try

These are the manual flows worth running once you have the console up. Each is meant to take 1–3 minutes; do not over-engineer.

**A. Silence.** Don't talk for 30 seconds. Expected: many `raw_audio_chunk` events, a few `vad_frame`, **no** `vad_turn_signal`-with-`p_done≥0.5`, **no** `SpeakDecision` with `action_type=full_response`. The graph closes after each ingested frame; orphan count stays at zero.

**B. Address the agent.** Say something like "hey companion, what's the weather like?" Expected: rising `p_continue` while you speak, then `p_done > 0.5` on the trailing silence, then a `SpeakDecision` row with `action_type=full_response` and `primary_reason_code=EOU_CONFIRMED`. The trace view shows a closed chain ending at the SpeakDecision.

**C. Talk past the agent (not addressed).** Say something like "ugh, this code is broken" — addressed to yourself, not the agent. Expected: `SpeakDecision` with `action_type=silence` and `primary_reason_code=NOT_ADDRESSED_TO_AGENT`. (`user_addressed_agent` is populated by the foreground-model `proposals_to_signals` path — see `companion_harness/realtime_orchestrator.py`.) This validates invariant #8 (silence wins ties).

**D. Thinking pause.** Say "I was thinking... [1.5s silence] ...maybe we should." Expected (v0.1b+ with `SmartTurnDetector` wired): no `full_response` fires during the pause; the agent waits. (If `SmartTurnDetector` isn't wired in your build, this scenario is informational only — the VAD-only build will incorrectly fire `full_response` at the pause. See issue #10 for context.)

**E. Backchannel.** While the agent is "speaking" (today, that means: while a `full_response` row is on screen), say "mm-hmm". Expected (v0.1b+ with `BackchannelClassifier`): a `backchannel` row appears, not an interruption. (Same caveat as D — VAD-only builds may misclassify.)

**F. Privacy mode.** Set the privacy-mode toggle on the page (or via a query string the Phase 1 PR defines) to `guest_present`. Speak something that would normally be memorable ("my favorite color is teal"). Expected: a `memory_commit_skipped` row, not a `memory_commit_completed` row, with the gate cited in the event payload. (Cross-references issue #113 — the chain should close fully; if you see a `memory_commit_completed` without a corresponding commit, that's the bug #113 tracks.)

**G. Forget command.** Say "forget that". Expected: an `explicit_forget` row, then a tombstone (`valid_to` set, not a hard delete). On `SessionStateStore` builds prior to the fix for issue #105, this will hard-delete instead — that's a known bug worth confirming or refuting.

**H. "Why did you say that?"** After any `full_response`, say "why did you say that?" Expected: the trace view should let you click the SpeakDecision and see the `retrieval_used` field populated with the `memory_retrieval_event` IDs that informed it. In v0.1e, retrieval is inert in the live flow (ASR isn't wired until Stage 5), so `retrieval_used` may be empty — that's expected, not a bug. The wiring is what matters.

### 2.6 Things to record

If you find a session interesting (good or bad), record three things:
- The session ID (printed in the startup banner and visible in the console).
- A quick description of what you did and what surprised you.
- The path to the blob store on b200 (e.g., `/tmp/manual_test_blobs/<session_id>/`). Don't `rm` it; we may want to replay it.

Open an issue per surprising finding. Tag it `manual-test-finding`. Don't bundle multiple findings into one issue.

### 2.7 Tearing down

Phase 1 server: `Ctrl-C` in the b200 terminal. `ssh -L` tunnel: `Ctrl-C` in the local terminal.

The blob store does not auto-clean. Delete `/tmp/manual_test_blobs/<session_id>/` on b200 once you no longer need replay; it is ephemeral.

---

## 3. Phase 2 — Audio + video walkthrough

> Status: Phase 2 lands in a separate PR after Phase 1 settles. This section is the contract for that PR. If you are reading this before Phase 2 has shipped, the video panel will not appear on the page yet.

### 3.1 What changes from Phase 1

- The capture page requests both `audio: true` and `video: true`.
- A new video panel renders a thumbnail strip — the last N frames sent to the server, with their `event_id` and `payload_hash`.
- A new fifth lane in the event log: **VisionSidecar events** (`vision_frame`, deictic-detection signals, audio-visual conflict scores).
- The video frame rate is intentionally low (~1 fps initially) — this is a manual-test rig, not a production camera path. Bandwidth is the constraint when streaming through `ssh -L`.

### 3.2 Start command

Same as Phase 1, with one extra flag (the Phase 2 PR pins the exact name):

```sh
/raid/yid042/venvs/companion-harness/bin/python3 -m manual_test_console.server \
    --host 0.0.0.0 --port 8800 --enable-video
```

The startup banner should include "VisionSidecar: wired".

### 3.3 New scenarios to try

**I. Deictic reference with no visual context.** Speak only — no camera grant. Say "what is this?" Expected: the same Phase-1 behavior; no visual signal flows; `deictic_reference=False` or a fallback. This validates that Phase 2 is strictly additive — the audio-only path still works when video is unavailable.

**J. Deictic reference with visual context.** Grant the camera, hold up an object, say "what is this?" Expected: a `vision_frame` row arrives; the foreground model's proposals may carry `deictic_reference=True`; SpeakPolicy may now consider the path through the deictic branch. The trace view shows the `caused_by[]` chain extending into a `vision_frame` predecessor.

**K. Audio-visual conflict.** Show the camera one thing while saying another ("look at this red apple" while holding a green one). Expected: `audio_visual_conflict_score` rises; if it crosses `0.7`, the SpeakDecision should be `action_type=clarification` with `primary_reason_code=AUDIO_VISUAL_CONFLICT`. This is the Stage 2 contract validated in vivo.

**L. Privacy gate `no_camera_memory`.** Toggle privacy mode to `no_camera_memory`. Hold up something memorable. Expected: the visual content does **not** enter memory; you should see either a `memory_commit_skipped` row or a complete absence of `memory_write_candidate` for visual content, per the privacy_gates contract in `companion_harness/privacy_gates.py`. Cross-references the `test_no_camera_memory` contract test.

### 3.4 Known limitations

- Frame rate is artificially low. Realistic vision performance is gated on the live-loop milestone, not this rig.
- No video output panel (e.g., a "this is what the agent thinks it sees" overlay). That's a v-future enhancement.
- VisionSidecar runs on b200 — the local browser only captures and sends. Browser CPU stays low.

---

## 4. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Page loads but no events appear | WebSocket didn't connect; check browser devtools Network → WS | Re-grant mic permission, check the `ssh -L` tunnel is still up |
| `vad_turn_signal` never fires | VAD threshold too high, mic gain too low | Verify mic input in browser; check `turn_detector_vad.py` config |
| Lots of `log_drop_or_degrade` events | EventLogger backpressure — drain not keeping up | Investigate; this is a bug, not a config issue. Save the session and file. |
| Orphan count > 0 in trace view | Invariant #1 violation — an event has no `caused_by[]` predecessor in the log | Save the session, file as a bug with the orphan event_ids |
| Server prints "EventLogger drain: blocked" | Drain task didn't start | Restart server; if it persists, the bug is in `event_logger.py` startup |
| Browser blocks mic for `http://localhost` | Some browsers require HTTPS for `getUserMedia` | Use Chrome; or set `chrome://flags/#unsafely-treat-insecure-origin-as-secure` for `http://localhost:8800` |
| `ssh -L` connects but page won't load | Port forward established but server isn't bound to `0.0.0.0` | Verify the `--host 0.0.0.0` flag; `--host localhost` only binds to b200's loopback |

If you see something not listed: save the session ID + blob store path + browser console log, and file an issue with all three.

---

## 5. What this rig is and isn't

**It is:** a low-fidelity, low-stakes observability surface so the lead can *experience* the harness running against live input and *see the events* the spec says should appear. It is the cheapest possible substitute for a "watch the flight recorder fly" view.

**It is not:**
- A demo. The console is technical; it is not designed for anyone but the lead.
- A latency benchmark. Network jitter on `ssh -L` will dominate any timing. Latency is measured against fixtures in the replay tests, not here.
- A test substitute. Contract tests still gate. A passing manual-test session is informational, not evidence of correctness. A failing one is evidence of a bug worth investigating.
- A spec milestone. Like `docs/manual-test-module-plan-draft.md` and `docs/visionclaw-adaptation-plan-draft.md`, this rig is parallel to the spec milestones — it doesn't move any gate. Invariants still bind.

---

## 6. Appendix — exact commands (pin these after Phase 1 PR lands)

After the Phase 1 PR merges, replace the placeholders below with the exact module path and flags it pins.

```sh
# b200 — start server
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server \
    --host 0.0.0.0 \
    --port 8800

# local — port-forward
ssh -L 8800:localhost:8800 b200

# local — open in browser
xdg-open http://localhost:8800/   # Linux
open http://localhost:8800/        # macOS

# b200 — smoke test (no browser; scripted WebSocket client)
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.smoke_client --chunks 100

# b200 — Phase 2 (audio + video)
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server \
    --host 0.0.0.0 \
    --port 8800 \
    --enable-video
```

---

## 7. Where this fits

- `docs/manual-test-module-plan-draft.md` — the build plan this handbook operationalizes.
- `docs/visionclaw-adaptation-plan-draft.md` — the wire contract (§4) the ingest endpoint follows.
- `docs/milestone-live-loop-integration-draft.md` — the **separate** milestone that adds synthesized voice back; this handbook does NOT cover that loop.
- `docs/architecture-v0.1.md` — the frozen spec. Everything in this handbook is consistent with it; nothing in this handbook overrides it.
- `docs/remote-dev.md` — the local↔b200 workflow this handbook depends on.
