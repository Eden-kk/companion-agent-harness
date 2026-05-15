# Manual-test handbook

Last updated: **2026-05-15** (post-manual-test-session). Originally written alongside Phase 1 service build; rewritten after the 2026-05-15 session against the live b200 deployment to reflect what actually happens.

> ## Finding 6 — CLOSED by PR #149
>
> Finding 6 (`user_addressed_agent` never flipping True) was resolved by PR #149, which wired `AddressingClassifier` into `StreamingRealtimeOrchestrator._policy_gate_task` (lines 423–431). The 3-tier classifier behavior:
>
> 1. **Wake-word tier**: `user_addressed_agent = True` iff "Claude" / "Claudia" is detected in the utterance (`AddressingClassifier` wake-word path; see `companion_harness/addressing_classifier.py`).
> 2. **Diarization-derived `social_mode` tier**: when no wake-word is found, `user_addressed_agent` uses diarization-derived `social_mode` to infer whether the agent is the intended recipient.
> 3. **Mechanical fallback**: when no wake-word and `social_mode` is `solo`, `user_addressed_agent = True` (single-human session; agent is the only available listener).
>
> Scenarios B, D, E no longer carry the "⚠️ Finding-6 affected" caveat. Use the wake-word ("Claude" or "Claudia") to address the agent and expect `action_type=full_response` when EOU is confirmed and the utterance is addressed.

This handbook is what the project lead reads when they want to actually use the harness with their voice (Phase 1: audio-only) and later with their voice + camera (Phase 2: audio + video). It is **not** a substitute for the architecture spec — it is the runbook that complements it.

If you are looking for *what the harness is for*, read `docs/architecture-v0.1.md`. If you are looking for *how to run a session and what to expect*, read this.

---

## 0. Scope and honesty

**Phase 1 — Audio-only manual test.** You speak into your laptop mic. The harness on b200 ingests audio, runs real VAD + Smart Turn + ASR + backchannel classification, runs SpeakPolicy, emits SpeakDecisions, generates speech with Kokoro TTS, and streams audio back to the browser. The plumbing for **voice-back is wired end-to-end** as of PRs #125/#127/#132/#135. The console shows you everything, including inline `action_type` + `primary_reason_code` on every decision row (PR #133).

The catch: as of the 2026-05-15 session, **the policy never selects `full_response`** (see headline box above — Finding 6). So although the TTS/audio-out path is wired and tested, in practice you will hear silence. Diagnosis of that gap is a follow-up.

What you can validate in Phase 1 today:

| Capability | Status | Notes |
|---|---|---|
| Causal graph closure (`harness_init → session_open → raw_audio → vad_turn_signal → SpeakDecision`) | ✅ Validates | Orphan count should stay at 0. |
| Real Silero VAD (ONNX) emitting `vad_turn_signal` with `p_done` / `p_continue` | ✅ Validates | PR #126. Replaces the energy stub. |
| Real Pipecat Smart Turn v3 (ONNX, CPU) suppressing mid-pause EOU | ✅ Validates | PR #126. |
| Real whisper-tiny.en ASR (faster-whisper) populating `user_transcript` on EOU | ✅ Validates | PR #136. Deterministic: temperature=0, beam_size=1. |
| Real backchannel classifier (whisper-tiny + lexicon, `emit_threshold=0.3`) | ✅ Validates | PRs #126, #134. Drain-storm fixed. |
| Inline `action_type` + `primary_reason_code` on `policy_decision` rows in console | ✅ Validates | PR #133. No need to fetch the blob. |
| Panel routing (left = `raw_audio`/`raw_video`; right = everything else) | ✅ Documented | §2.4, per PR #131. |
| Real MiniCPM-o 4.5 foreground on b200 GPU | ✅ Loaded | PR #125 wired the orchestrator. |
| Real Kokoro-82M-ONNX TTS + `WebSocketAudioSink → /ws/audio_out → browser AudioContext` | ✅ Wired | PRs #127, #132, #135. |
| EventLogger non-blocking on realtime path | ✅ Validates | Backpressure surfaces as `log_drop_or_degrade`. |
| Policy actually selecting `full_response` end-to-end | ❌ **Blocked by Finding 6** | All upstream signals fire, but `user_addressed_agent` never asserts. See headline box. |
| Privacy gates writing to a real memory store | ⚠️ Partial | Gates fire (`memory_commit_skipped` observable); persistent store wiring is a separate follow-up. |
| Direct-question latency end-to-end | ❌ Unmeasurable today | No `full_response` → no end-to-end timing yet. |
| Barge-in cutting voice mid-utterance | ❌ Unmeasurable today | Requires `full_response` to interrupt. |

**Phase 2 — Audio + video manual test.** Vision capture works (PR #123 — `raw_video_frame` events emitted). VisionSidecar wiring into the foreground model is **landed** (v0.1h Task 2). Pass `--enable-vision` (default OFF) to enable a per-session `VisionSidecar` that buffers the most-recent frame and pairs it with the next audio chunk so MiniCPM-o's vision tower runs once per frame. Scene-change scoring and deictic grounding return stub values (0.0 / False) until v0.1j.

---

## 1. Prerequisites

- **A working b200 SSH connection.** `ssh b200` should succeed without a password prompt for everyday use. Verify on first use; see `docs/remote-dev.md`.
- **A laptop with a working mic** (and webcam for Phase 2). Browser permissions for mic/camera will be requested on first open.
- **Chrome, Firefox, or Safari (recent).** The console uses `getUserMedia` + `WebSocket`; the audio-out path additionally uses `AudioContext`.
- **The canonical venv on b200** at `/raid/yid042/venvs/companion-harness/` with all `requirements.txt` deps installed. Confirm with `/raid/yid042/venvs/companion-harness/bin/python3 -c "import websockets, aiohttp, faster_whisper, onnxruntime"`.
- **The repo on b200** at `~/companion-agent-harness/` at a recent commit. After a relevant PR merges, `git pull origin main` on b200 + restart is enough.

If any prerequisite is not in place, fix it before continuing. Don't paper over it.

---

## 2. Phase 1 — Audio-only walkthrough

### 2.1 Start the server on b200

As of 2026-05-15, the server is **already running** on b200 as PID `1851682`, port 8800. You normally don't need to restart it. Confirm with:

```sh
ssh b200 ss -ltnp | grep :8800
```

The startup banner (visible in the server log) should show real-model labels:

```
VAD: Silero (ONNX)
SmartTurn: Pipecat Smart Turn v3 (ONNX, CPU)
Backchannel: whisper-tiny + lexicon (emit_threshold=0.3)
ASR: whisper-tiny.en (faster-whisper)
TTS: Kokoro-82M-ONNX
Foreground: MiniCPM-o 4.5 (b200 GPU)
```

If any line says "stub" or "constant", a PR didn't deploy correctly — see §6 for the restart command.

### 2.2 Forward the port to your laptop

In a local terminal:

```sh
ssh -L 8800:localhost:8800 b200
```

This forwards local `:8800` → b200's `:8800`. Keep this shell open. (Cloudflare quick tunnel is the alternative if `ssh -L` is inconvenient — see `docs/manual-test-module-plan-draft.md` §6.)

### 2.3 Open the console

In your browser: `http://localhost:8800/`.

You should see the manual-test console page. Click **Grant mic** when prompted. The page should immediately start streaming PCM16 audio over the WebSocket and showing rows in the event panel. If audio-back works (i.e., Finding 6 has been fixed and policy approves a `full_response`), the page will also play synthesized audio through `AudioContext` — grant any audio-autoplay prompts the browser raises.

### 2.4 What you should see

The page renders two panels:

1. **Ingested input events (left panel)** — `payload_kind ∈ {raw_audio, raw_video}`. So: `raw_audio_chunk` rows with `seq_no`, `payload_hash`, and `caused_by[]`. New rows arrive ~10×/second. Once Phase 2 wiring lands, `raw_video_frame` rows appear here too.
2. **TurnSignals / SpeakDecisions / causal chain (right panel)** — everything else: `vad_frame`, `vad_turn_signal` (with `p_done`, `p_continue`, `confidence`), `smart_turn_signal`, `backchannel_classification`, `asr_transcript`, `policy_decision` (with **inlined** `action_type` + `primary_reason_code` per PR #133), foreground proposals, memory events, `log_drop_or_degrade`.

**Panel routing.** The split is by `payload_kind` (PR #131). If you're watching for a `SpeakDecision` or `vad_turn_signal`, look right. If you're watching mic capture, look left.

**Causal graph trace.** Each decision row is clickable and opens its decision_trace blob. The DAG must close — orphan count stays at zero. If it climbs, that's an invariant #1 violation worth reporting.

### 2.5 Scripted scenarios to try

These are the manual flows worth running. Each is meant to take 1–3 minutes; do not over-engineer. The **⚠️ Finding-6 affected** marker means the upstream signals are observable today but the final `full_response` action will never fire until Finding 6 is diagnosed.

**A. Silence.** Don't talk for 30 seconds. Expected (no change pre/post Finding 6): many `raw_audio_chunk` events; a few `vad_frame`; **no** `vad_turn_signal`-with-`p_done≥0.5`; `policy_decision` rows show `action_type=silence` with `primary_reason_code=NOT_ADDRESSED_TO_AGENT`. The graph closes; orphan count stays at zero.

**B. Address the agent.** Say "hey companion, what's the weather like?" Expected (pre-Finding-6): a `SpeakDecision` row with `action_type=full_response` and `primary_reason_code=EOU_CONFIRMED`. Actual today: `vad_turn_signal` with rising `p_done` fires correctly; Smart Turn fires; `asr_transcript` populates `user_transcript`; but the `policy_decision` row shows `action_type=silence / NOT_ADDRESSED_TO_AGENT`. ⚠️ **Finding-6 affected.** Verify the upstream signals fire; mark the gap.

**C. Talk past the agent (not addressed).** Say "ugh, this code is broken" — addressed to yourself, not the agent. Expected: `action_type=silence / NOT_ADDRESSED_TO_AGENT`. ⚠️ **Finding-6 affected** (correct outcome reached, but vacuously — every utterance currently produces this).

**D. Thinking pause.** Say "I was thinking... [1.5s silence] ...maybe we should." Expected: Smart Turn v3 IS real now (PR #126) and observably suppresses the mid-pause EOU — watch for a `smart_turn_signal` event with `p_done < 0.5` on the pause. ⚠️ **Finding-6 affected**: the `full_response` on the continuation won't fire. Smart Turn behavior is observable; `full_response` gating is not verifiable end-to-end.

**E. Backchannel.** Say "mm-hmm" or "yeah". Expected: a `backchannel_classification` event with a non-zero `p_backchannel` value. The `emit_threshold=0.3` gate (PR #134) keeps noise-floor frames out, so any event you see is a real classification, not chatter. ⚠️ **Finding-6 affected** for the policy action — you will not see the agent treat it as a non-interrupting acknowledgement, because no `full_response` is in flight to acknowledge.

**F. Privacy mode.** Set the privacy-mode toggle to `guest_present`. Say something memorable ("my favorite color is teal"). Expected: a `memory_commit_skipped` row, with the gate cited in the event payload. ⚠️ Caveat: the persistent memory store is **not yet wired into the live pipeline** — gates fire and emit the skip event, but there is no live writer to skip past. This is a separate follow-up.

**G. Forget command.** Say "forget that". Expected: ASR now populates `_detect_explicit_remember`/`_detect_explicit_forget` (PR #136), so an `explicit_forget` event should fire if the parser matches. ⚠️ Partial: as with F, the memory store wiring is still pending, so no tombstone is persisted. The detection event is the observable signal.

**H. "Why did you say that?"** After any `policy_decision` row (today: only `silence` or `backchannel`), click it. Expected: the row opens its decision_trace blob and shows the `PolicyInputs` snapshot, the proposal candidates, and the gate evaluations. Decision traces ARE persisted and clickable. ⚠️ Caveat: retrieval is wired but the query string is empty in the live path — the "why did you say that?" utterance isn't yet tied to a retrieval query, so `retrieval_used` will be empty. The wiring is what matters; the query routing is a follow-up.

### 2.6 Things to record

If you find a session interesting (good or bad), record three things:
- The session ID (printed in the startup banner and visible in the console).
- A quick description of what you did and what surprised you.
- The path to the blob store on b200 (e.g., `/tmp/manual_test_blobs/<session_id>/`). For the 2026-05-15 session, decision traces live at `/tmp/manual_test_blobs/decision_traces/`. Don't `rm` them; we may want to replay.

Open an issue per surprising finding. Tag it `manual-test-finding`. Don't bundle multiple findings into one issue.

### 2.7 Tearing down

Phase 1 server: it normally stays running. If you do need to stop it, `kill <PID>` on b200 (current PID is `1851682`). `ssh -L` tunnel: `Ctrl-C` in the local terminal.

The blob store does not auto-clean. Delete `/tmp/manual_test_blobs/<session_id>/` on b200 once you no longer need replay; it is ephemeral.

**Memory slate hygiene.** Each session now creates per-session memory directories under `<blob_dir>/<session_id>/memory/{session,core,episodic,semantic}/`. If you want a completely clean memory slate before a new session (no residual store directories from prior runs), run:

```bash
rm -rf /tmp/manual_test_blobs/*   # before each session
```

This is operator-managed; the server does not auto-clean between sessions.

---

### 2.8 Threshold-tuning dashboard

The dashboard is a collapsible tuning panel served at `http://localhost:8800/` alongside the main event console (PR #153). It lets you adjust the 12 Tier-B runtime thresholds without restarting the server. Every change is recorded as an auditable `config_change` event — the dashboard is a first-class audit surface, not a side-channel.

### 2.8.1 Opening the panel

Navigate to `http://localhost:8800/` (same URL as the main console; port 8800 is canonical — do not use 8000). The tuning panel appears in the right column by default. To collapse it, click `[≪ hide]`; the panel shrinks to a `[⚙ Tuning]` header button. Click it again to expand. On screens ≤ 1024 px the panel slides over the right column; press `Esc` to close.

On load the panel issues `GET /config` to populate each slider with the server's current live value. If the panel shows dashes instead of numbers, the server isn't responding — check the `ssh -L` tunnel and the server process (`ss -ltnp | grep :8800` on b200).

### 2.8.2 The three sections

The panel has three sections. The 3-section grouping (Policy / Detectors / Orchestrator) is defined in the dashboard UI source at `manual_test_console/index.html` (search for `const TUNING_SECTIONS`); the canonical key list + ranges live in `manual_test_console/config_schema.py::ALLOWLIST` (12 knobs total; the set is spec-locked per Anchor 1 — see `docs/roadmap-v0.1i-draft.md`):

| Section | Count | Keys | Default expand |
|---|---|---|---|
| **Policy** | 3 | `policy.backchannel_threshold`, `policy.audio_visual_conflict_threshold`, `policy.grounding_confidence_threshold` | expanded |
| **Detectors** | 5 | `detectors.vad.speech_threshold`, `detectors.vad.silence_onset_ms`, `detectors.smart_turn.silence_onset_ms`, `detectors.smart_turn.silence_rms_threshold`, `detectors.backchannel.emit_threshold` | expanded |
| **Orchestrator** | 4 | `orchestrator.proposal_batch_window_ms`, `orchestrator.hard_cancel_after_ms`, `orchestrator.p_speech_thresh`, `orchestrator.p_backchannel_thresh` | collapsed |

Each slider row shows: label, current numeric value, and `[min … max]` range (from `GET /config`).

### 2.8.3 Drag-to-tune semantics

Drag the slider thumb to a new position. The readout updates locally as you drag; the `POST /config/patch` fires on mouse/touch release. Visual states during a drag-and-release cycle:

- **At default (gray):** value matches schema default.
- **Modified (blue + bold + `(modified)` badge + active `[↺]`):** value differs from default; not yet submitted.
- **Pending (`⏳` + knob pulse):** `POST /config/patch` in flight.
- **Accepted:** server returned 200; row returns to normal color with `(modified)` badge if the new value is still non-default.
- **Rejected (red row-flash + toast for 3 s + revert):** patch was refused. The slider reverts to the previous value.

Rejection routing (PR #151):

| Rejection reason | HTTP status | Toast message |
|---|---|---|
| Key is in Tier-A list (spec-pinned) | 403 | `Tier A — key not tunable` |
| Key unknown (not in Tier-B allowlist) | 403 | `Unknown key` |
| Value out of `[min, max]` range | 400 | `Out of range: [min, max]` |

Important: rejected patches emit **neither** `operator_action` nor `config_change` — all three rejection branches (Tier-A 403 / unknown-key 403 / out-of-range 400) return before the event-emission call in `manual_test_console/server.py`. Only the HTTP status code signals rejection; the event log is silent. To know whether a patch was rejected, check the HTTP response — not the audit tail.

### 2.8.4 Audit tail click-to-jump

The panel footer shows the last 5 `config_change` events as a scrollable audit tail (most-recent first). Each entry is clickable: clicking it jumps to the corresponding event row in the right-column event log and highlights it.

Event emission contract (one per accepted patch, per PR #151 numeric gate):

- **Every accepted `POST /config/patch` emits exactly one `operator_action` followed by exactly one `config_change`.** The `config_change` payload carries `{key, previous_value, new_value, applied_at_ms, operator_action_event_id}`. The `operator_action` carries `{endpoint, client_ip, request_id, action_type, rejection_reason?}`. Both events use `retention_policy_id="config_change_30d"`.
- The `config_change` event's `caused_by[]` includes the `operator_action` event's `event_id`, so the DAG closes (invariant #1).

### 2.8.5 Reset semantics

Three granularities of reset:

- **Per-slider `[↺]`** — reverts one key to its schema default. Visible only when the slider is in the modified state.
- **Per-section `[reset]`** — reverts all keys in the section.
- **Global `[reset all]`** — issues `POST /config/reset` and reverts every Tier-B key.

Asymmetric event emission on reset (PR #151 contract):

- A key that is already at its default when reset fires still emits one `operator_action` (the request is recorded) but **no** `config_change` (nothing changed).
- A key that was modified emits `operator_action` + `config_change` exactly once each.
- A `POST /config/reset` that reverts N modified keys emits N `config_change` events plus a single summary `operator_action`.

### 2.8.6 When a key change does not take effect immediately

The three sections have different propagation timing (PR #152 adapter wiring):

- **Policy thresholds** (`policy.*`, 3 keys): apply within the **current EOU decision** — the orchestrator reads the ConfigStore with a no-await snapshot at EOU time, so the new value is live before the `SpeakDecision` is emitted.
- **Detector thresholds** (`detectors.*`, 5 keys): apply starting on the **next frame after the current EOU** — the detectors snapshot the store once per frame, not mid-frame.
- **Orchestrator timing** (`orchestrator.*`, 4 keys): apply at the **next batch boundary** — the orchestrator reads these at the start of each proposal-batch window.

For manual testing this is rarely noticeable, but if you change a threshold and the next event doesn't reflect the new value, wait one more turn and check again.

### 2.8.7 Finding the audit trail after a session

Decision trace blobs at `/tmp/manual_test_blobs/<session_id>/` include `config_change` events in the same event log as `policy_decision` events. When a `config_change` is in scope at decision time, its `event_id` appears in the `caused_by[]` of subsequent `policy_decision` events — the causal chain is closed. To walk the chain:

```sh
# On b200, list the event log for a session:
ls /tmp/manual_test_blobs/<session_id>/

# Grep for config_change entries:
grep '"event_type": "config_change"' /tmp/manual_test_blobs/<session_id>/events.jsonl
```

The `operator_action_event_id` field in each `config_change` payload points back to the HTTP request that triggered it.

---

## 3. Phase 2 — Audio + video walkthrough

> Status (2026-05-15): vision capture works (PR #123 — `raw_video_frame` events emit). VisionSidecar wiring into the foreground model is **landed** (v0.1h Task 2). Start the server with `--enable-vision` (default OFF) to activate per-session sidecar pairing. Scene-change scoring and deictic grounding return stub values (0.0 / False) until v0.1j real models land.

### 3.1 What changes from Phase 1

- The capture page requests both `audio: true` and `video: true`.
- A new video panel renders a thumbnail strip — the last N frames sent to the server, with their `event_id` and `payload_hash`.
- `raw_video_frame` events appear in the left panel.
- When `--enable-vision` is **OFF** (default): frames are captured and `raw_video_frame` events emitted, but no `vision_frame` events follow — the foreground model receives `video=None` on every audio chunk.
- When `--enable-vision` is **ON**: the right panel gains `vision_frame` events (one per ingested frame, `caused_by=[raw_video_frame.event_id]`). The foreground model receives `(audio, video_bytes)` pairs — the most-recent frame fires once then is cleared (consume-once semantics). Scene-change scoring (`scene_change_score`) and deictic grounding (`deictic_reference`) return stub values (0.0 / False) until v0.1j.
- The video frame rate is intentionally low (~1 fps initially) — this is a manual-test rig, not a production camera path. Bandwidth is the constraint when streaming through `ssh -L`.

### 3.2 Start command

Same as Phase 1 with `--enable-vision` added:

```sh
HF_HUB_CACHE=/raid/huggingface/hub /raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs --enable-vision
```

The startup banner reports `Vision: ENABLED (init_vision=True, +~18 GB VRAM)`. The `/healthz` endpoint reports `vision_enabled`, `frames_buffered` (count across active sessions), and `last_frame_event_id`. Omit `--enable-vision` to stay on the audio-only path (startup banner shows `Vision: disabled`).

### 3.3 New scenarios to try

Scenarios I–L require `--enable-vision`. Without it you will see `raw_video_frame` events in the left panel but no `vision_frame` events and no vision-derived signals on the right. With `--enable-vision`, the expected behavior below is reachable (scoring/grounding stubs return 0.0/False until v0.1j).

**I. Deictic reference with no visual context.** Speak only — no camera grant. Say "what is this?" Expected: the same Phase-1 behavior; no visual signal flows; `deictic_reference=False` or a fallback. ⚠️ **Finding-6 affected**: even with no visual ambiguity, `full_response` will not fire today.

**J. Deictic reference with visual context.** Grant the camera, hold up an object, say "what is this?" Expected (post-VisionSidecar-merge): a `vision_frame` row arrives; the foreground model's proposals may carry `deictic_reference=True`; the trace view shows the `caused_by[]` chain extending into a `vision_frame` predecessor. ⚠️ **Finding-6 affected** for the final policy action.

**K. Audio-visual conflict.** Show the camera one thing while saying another ("look at this red apple" while holding a green one). Expected (post-VisionSidecar-merge): `audio_visual_conflict_score` rises; if it crosses `0.7`, the SpeakDecision should be `action_type=clarification` with `primary_reason_code=AUDIO_VISUAL_CONFLICT`. ⚠️ **Finding-6 affected**: today the policy never approves a non-silent action, so clarification won't fire either.

**L. Privacy gate `no_camera_memory`.** Toggle privacy mode to `no_camera_memory`. Hold up something memorable. Expected (post-VisionSidecar-merge): the visual content does **not** enter memory; you should see either a `memory_commit_skipped` row or a complete absence of `memory_write_candidate` for visual content, per the privacy_gates contract in `companion_harness/privacy_gates.py`. ⚠️ Same caveat as scenario F — persistent memory store wiring is pending.

### 3.4 Known limitations

- Frame rate is artificially low. Realistic vision performance is gated on the live-loop milestone, not this rig.
- No video output panel (e.g., a "this is what the agent thinks it sees" overlay). That's a v-future enhancement.
- VisionSidecar runs on b200 — the local browser only captures and sends. Browser CPU stays low.
- `--enable-vision` loads MiniCPM-o with `init_vision=True` (+~18 GB VRAM on b200). Default OFF preserves the audio-only path unchanged.

---

## 4. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| **Every utterance comes back as `silence`** | **Known: Finding 6.** Open architectural issue. 530/588 silence rows in the 2026-05-15 session; `user_addressed_agent` never flips True. Path of investigation: `proposals_to_signals` in `companion_harness/realtime_orchestrator.py` and the proposal-text inspection in `companion_harness/foreground_model_minicpm.py`. ASR is wired (PR #136) but the proposal path may not be consuming it. | Don't file a new bug — track Finding 6 instead. Save the session blob path so it can be cross-referenced. |
| `backchannel_classification` rows with `p_backchannel=0` | Shouldn't happen: emit threshold (0.3) suppresses noise-floor frames (PR #134). | If you do see them, the threshold is misconfigured. Check the server config and file a bug. |
| Browser plays nothing even when a `full_response` fires | Check `/healthz` for `audio_out_chunks_sent`. If 0, policy never approved speech (likely Finding 6). If > 0 but silent: check the browser console for `AudioContext` permission/decode errors; some browsers block autoplay until user interaction. | Reload the page, click somewhere on it first, retry. |
| Page loads but no events appear | WebSocket didn't connect; check browser devtools Network → WS | Re-grant mic permission, check the `ssh -L` tunnel is still up |
| `vad_turn_signal` never fires | Mic gain too low; or silence sustained beyond Silero's window | Verify mic input in browser; check `companion_harness/turn_detector_vad.py` config; speak louder/closer |
| Lots of `log_drop_or_degrade` events | EventLogger backpressure — drain not keeping up. **Note:** the drain-storm bug from earlier sessions was fixed in PR #134; if you see this now, it's a NEW bug. | Save the session and file. Don't dismiss as the old issue. |
| Orphan count > 0 in trace view | Invariant #1 violation — an event has no `caused_by[]` predecessor in the log | Save the session, file as a bug with the orphan event_ids |
| Server banner shows a "stub" label for any model | The deployed commit predates the relevant real-model PR (#126/#127/#136 etc.) | `git pull` on b200 and restart per §6 |
| Server prints "EventLogger drain: blocked" | Drain task didn't start | Restart server; if it persists, the bug is in `event_logger.py` startup |
| Browser blocks mic for `http://localhost` | Some browsers require HTTPS for `getUserMedia` | Use Chrome; or set `chrome://flags/#unsafely-treat-insecure-origin-as-secure` for `http://localhost:8800` |
| `ssh -L` connects but page won't load | Port forward established but server isn't bound to `0.0.0.0` | Verify the `--host 0.0.0.0` flag; `--host localhost` only binds to b200's loopback |
| Slider rejects with a red flash / toast says `Tier A` | You hit the Tier-A allowlist guard (PR #151). Tier-A keys are spec-pinned. | See `docs/design-config-and-dashboard.md` §1 Tier A. Making a key tunable means moving it to the Tier-B allowlist, which requires an event-schema migration and a new `ALLOWLIST` entry. |

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

## 6. Appendix — exact commands

```sh
# b200 — server is already running as of 2026-05-15.
# Banner should show:
#   VAD: Silero (ONNX) ... SmartTurn: Pipecat Smart Turn v3 (ONNX, CPU)
#   Backchannel: whisper-tiny + lexicon (emit_threshold=0.3)
#   TTS: Kokoro-82M-ONNX ... ASR: whisper-tiny.en (faster-whisper)
#   Foreground: MiniCPM-o 4.5 (b200 GPU)
# Confirm running:
ss -ltnp | grep :8800   # current PID is 1851682

# If you need to restart (after a PR merge):
kill <PID>   # e.g. kill 1851682
HF_HUB_CACHE=/raid/huggingface/hub /raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs

# With VisionSidecar (v0.1h Task 2 — landed), enable via:
HF_HUB_CACHE=/raid/huggingface/hub /raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs --enable-vision

# local — port-forward
ssh -L 8800:localhost:8800 b200

# local — open in browser
xdg-open http://localhost:8800/   # Linux
open http://localhost:8800/        # macOS

# b200 — smoke test (no browser; scripted WebSocket client)
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.smoke_client --chunks 100
```

---

## 7. Where this fits

- `docs/manual-test-module-plan-draft.md` — the build plan this handbook operationalizes.
- `docs/visionclaw-adaptation-plan-draft.md` — the wire contract (§4) the ingest endpoint follows.
- `docs/milestone-live-loop-integration-draft.md` — the milestone that wired voice-back end-to-end. Voice-back is now present in the harness; the speak-decision gating (Finding 6) is the remaining open issue.
- `docs/research-asr-models-2026-05-15.md` — the ASR-model selection rationale behind PR #136.
- `docs/plan-vision-sidecar-wiring.md` — the converged VisionSidecar plan; implemented by v0.1h Task 2.
- `docs/architecture-v0.1.md` — the frozen spec. Everything in this handbook is consistent with it; nothing in this handbook overrides it.
- `docs/remote-dev.md` — the local↔b200 workflow this handbook depends on.
- `docs/design-config-and-dashboard.md` — the design doc behind the threshold-tuning dashboard (PR #143 is open; once merged, the doc will live at that path).

**Relevant PRs that turned components real:**
- PR #123 — vision capture (`raw_video_frame` events emitted).
- PR #125 — orchestrator wired into the manual-test server; VAD signals + SpeakDecisions reach the right panel.
- PR #126 — real Silero VAD (ONNX), Pipecat Smart Turn v3 (ONNX, CPU), whisper-tiny + lexicon backchannel.
- PR #127 — Kokoro-82M-ONNX TTS adapter wired.
- PR #131 — panel-routing documentation in §2.4.
- PR #132 — `WebSocketAudioSink → /ws/audio_out → browser AudioContext` audio-out path.
- PR #133 — inlined `action_type` + `primary_reason_code` on `policy_decision` event rows.
- PR #134 — backchannel `emit_threshold=0.3` gate; fixes drain-storm.
- PR #135 — closed the live-loop integration between policy → TTS → audio sink.
- PR #136 — `whisper-tiny.en` ASR via faster-whisper; populates `PolicyInputs.user_transcript` on EOU.
- v0.1h Task 2 — VisionSidecar per-session wiring + `--enable-vision` flag (landed).
