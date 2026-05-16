# Manual-test handbook

Last updated: **2026-05-15** (post-v0.1j stub-realization sweep). This is the runbook for actually using the harness with your voice + camera. It is not a substitute for the architecture spec.

If you want *what the harness is for*: read [`architecture-v0.1.md`](architecture-v0.1.md).
If you want *the model + flow at a glance*: read [`model-stack.md`](model-stack.md).
If you want *how to run a session and what to expect*: keep reading.

> ## Status banner — 2026-05-15
>
> - Addressing classifier (Finding 6) **is wired and live**: MiniCPM-driven primary path + wake-word ("Claude" / "Claudia") safety net + mechanical `solo` fallback. `user_addressed_agent=True` now fires for addressed utterances.
> - Voice-back path **works end-to-end**: Silero VAD → SmartTurn v3 → faster-whisper-tiny → MiniCPM addressing → 4-store memory retrieve → SpeakPolicy v0.1j → MiniCPM-o proposal → Kokoro TTS → browser playback.
> - Tier-B tuning dashboard is live in the right panel of the console.
> - 8 model stubs still return neutral values; see `model-stack.md` for the catalog. Replacement PRs for #166/#168/#169/#171/#172/#183/#188/#213 are in flight.

---

## 0. What you can validate today

**Phase 1 — Audio-only.** You speak into your laptop mic. The harness on this machine (b200) ingests audio, runs the full T1→T2→T3→T4 pipeline, and streams Kokoro-synthesized audio back to the browser. The console event panel shows every step with its `caused_by[]` predecessor.

| Capability | Status | Notes |
|---|---|---|
| Causal graph closure (`harness_init → session_open → raw_audio → ... → SpeakDecision`) | ✅ | Orphan count stays at 0. |
| Silero VAD (real, ONNX) — `vad_turn_signal` with `p_done`/`p_continue` | ✅ | |
| Pipecat SmartTurn v3 (real, ONNX, CPU) — mid-pause EOU suppression | ✅ | |
| MiniCPM native_duplex EOU (final primary EOU producer) | ✅ | Wired by v0.1j Task 8. `signal_producer_fallback` emits on fallback. |
| Backchannel classifier (real, whisper-tiny + lexicon, `emit_threshold=0.3`) | ✅ | |
| ASR (real, faster-whisper-tiny) — populates `user_transcript` on EOU | ✅ | Deterministic: temperature=0, beam_size=1. |
| MiniCPM addressing classifier (real, primary) + WakeWord (real, safety net) | ✅ | Wired by v0.1j Task 9. |
| SpeakPolicy.decide() v0.1j (8 paths, RUBRIC + ATTACHMENT_RISK gates, tool_status path) | ✅ | POLICY_VERSION = `v0.1j`. Bit-identical Tier-B replay. |
| MiniCPM-o-2.6 foreground proposal generation | ✅ | `init_vision`/`init_audio`/`init_tts` all True. |
| Kokoro-82M-ONNX TTS → `/ws/audio_out` → browser `AudioContext` | ✅ | Default. Switch via `--tts-adapter native_minicpm`. |
| 4-store memory (session/core/episodic/semantic) per session | ✅ | Per-session dirs under `<blob_dir>/<session_id>/memory/`. |
| Lexical memory retrieval populating `context_items` into foreground | ✅ | Embedding-based retrieval pending PR for #183. |
| SleepTimeAgent (opt-in) batches `memory_write_candidate` → commits | ✅ | Wire via `wire_sleep_time_agent=True`. Provenance = stub or MiniCPM (#188 PR in flight). |
| `forget that` / `remember X` detection in transcript | ✅ | Wired in v0.1h Task 1. Persisted tombstone wiring pending. |
| Tool-routing fast path + 300ms barge-in cancellation | ✅ | v0.1f. `tool_call_*` event chain closes. |
| Filler-budget state machine (2 fillers, 4s gap, evidence-bound) | ✅ | `ToolProgressEmitter` with pure `evidence_at()` for replay. |
| RegexAestheticRubric gate (8 violation IDs) | ✅ | v0.1g. `RUBRIC_VIOLATION` ReasonCode. |
| EventStreamAttachmentRiskMonitor → `ATTACHMENT_RISK_DAMPEN` | ✅ | v0.1g + #213 wiring (PR #234 in flight; pre-wire returns 0.0). |
| Tier-B threshold dashboard (12 knobs, live patch, audit events) | ✅ | Right column of console. |
| EventLogger async non-blocking + `log_drop_or_degrade` on backpressure | ✅ | Realtime path never waits on log durability. |
| `EventLogger.late_subscribe()` API for off-stream subscribers | 🟡 | Recovery PR #241 in flight; legacy `.subscribe()` still works. |
| Tier-B replay (`run_tier_b_replay()`, `assert_bit_identical()`) | ✅ | `companion_harness/replay.py`. |
| Eval Phase A (harness_native adapter + CLI + reporters) | ✅ | `pip install -e .[eval]` → `python -m companion_harness.evals run --adapter harness_native`. |
| Eval Phase A.5 (synthetic clock + fixture driver) | ✅ | |
| Eval Phase B1/B2 (CANDOR + FullDuplexBench synthetic case sources) | ✅ | Real data adapters deferred. |
| Eval Phase D (VoiceBench + VocalBench + HumDial-FDBench, synthetic-mode) | 🟡 | VocalBench merged (#232). VoiceBench (#233), HumDial-FDBench in flight. |

**Phase 2 — Audio + video.** Same audio path plus per-session `VisionSidecar`. Enable with `--enable-vision` (default OFF; loads MiniCPM-o with `init_vision=True`, +18 GB VRAM).

| Vision capability | Status | Notes |
|---|---|---|
| Frame capture → `raw_video_frame` events | ✅ | |
| `VisionSidecar` per-session buffer (consume-once, last-writer-wins) | ✅ | v0.1h Task 2. |
| `vision_frame` events with `caused_by=[raw_video_frame.event_id]` | ✅ | |
| Video frame reaches MiniCPM-o vision tower (paired with next audio chunk) | ✅ | |
| `no_camera_memory` privacy gate (first-line check in `ingest_frame_bytes`) | ✅ | |
| Scene-change scorer | 🟡 stub returns 0.0 | Real CLIP impl in flight (#166). |
| Grounding model | 🟡 stub returns 0.0 | Real Grounding-DINO impl in flight (#172). |
| Audio-visual conflict scorer | 🟡 stub returns 0.0 | Heuristic impl in flight (#168). |
| Deictic reference detector | 🟡 stub returns `(False, 0.0)` | MiniCPM-reused impl in flight (#169). |
| Urgency scorer | 🟡 stub returns 0.0 | Prosody+lexicon impl in flight (#171). |

---

## 1. Prerequisites

- **Working canonical venv** at `/raid/yid042/venvs/companion-harness/`. Confirm with:
  ```bash
  /raid/yid042/venvs/companion-harness/bin/python3 -c "import websockets, aiohttp, faster_whisper, onnxruntime"
  ```
- **Recent main checkout** of the repo. After a relevant PR merges, `git pull origin main` and restart the server.
- **Laptop with working mic** (and webcam for Phase 2). Browser asks for permissions on first open.
- **Chrome, Firefox, or Safari (recent).** Uses `getUserMedia` + WebSocket; audio-out uses `AudioContext`.

If anything is missing, fix it before continuing.

---

## 2. Phase 1 — Audio-only walkthrough

### 2.1 Start the server

```bash
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server \
    --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs
```

Default flags load MiniCPM-o (vision off, +0 GB extra), real Silero/SmartTurn/whisper-tiny detectors, and Kokoro TTS. The startup banner reports:

```
manual-test console — Phase 3 (live-loop pipeline wiring, no voice-back)
  bind:        0.0.0.0:8800
  blob store:  /tmp/manual_test_blobs
  live loop:   ENABLED (loading MiniCPM-o + real detectors at startup)
  sessions:    VAD, SmartTurn, Backchannel, SpeakPolicy, MiniCPM proposals
  Vision:      disabled
  open page:   http://localhost:8800/
```

Cold load takes ~60–120 s (MiniCPM-o checkpoint + Silero/SmartTurn ONNX + Kokoro). After "live pipeline ready" is printed, the server is responsive.

To check whether it's already running:
```bash
ss -ltnp | grep :8800     # or: lsof -i :8800
```

### 2.2 Open the console

Browser → `http://localhost:8800/` (or via SSH port-forward if you're on a laptop: `ssh -L 8800:localhost:8800 b200`).

Click **Grant mic** when prompted. PCM16 audio starts streaming over the WebSocket; rows appear in the event panel. If a `full_response` decision fires, the page also plays synthesized audio via `AudioContext` — grant any audio-autoplay prompt the browser raises.

### 2.3 What you see

**3-column layout** (right column collapses on smaller screens):

| Panel | Content |
|---|---|
| **Left** — ingested input events | `raw_audio_chunk` (`seq_no`, `payload_hash`, `caused_by[]`), `raw_video_frame` when vision enabled. ~10 rows/sec for audio. |
| **Middle** — display panel | Causal-chain events: `vad_frame`, `vad_turn_signal` (with `p_done`/`p_continue`/`confidence`), `smart_turn_signal`, `backchannel_classification`, `asr_transcript_emitted`, `addressing_classified`, `memory_retrieval_event`, `policy_decision` (with inline `action_type` + `primary_reason_code`), `foreground_proposal`, `tts_audio_emitted`, `log_drop_or_degrade`, `signal_producer_fallback`. Video tile + thumbnail strip when vision enabled. |
| **Right** — tuning dashboard | 12 Tier-B knobs (3 policy / 5 detectors / 4 orchestrator). Slider patch → `POST /config/patch` → `operator_action` + `config_change` events. See §2.8. |

**Causal trace.** Each `policy_decision` row is clickable: opens the DecisionTrace blob with `PolicyInputs` snapshot, proposal candidates, gate evaluations. The DAG must close — if orphan count climbs above 0, it's an invariant #1 violation worth reporting.

### 2.4 Scripted scenarios

Each scenario is meant to take 1–3 minutes. Watch the event panel and decision rows.

**A. Silence.** Don't talk for 30 seconds. Expected: many `raw_audio_chunk`; few `vad_frame`; no `vad_turn_signal` with `p_done≥0.5`; `policy_decision` rows show `action_type=silence` with `primary_reason_code=NOT_ADDRESSED_TO_AGENT` (or no decisions at all). Orphan count stays 0.

**B. Address the agent (wake-word path).** Say "Claude, what's the weather like?" Expected: `vad_turn_signal` with rising `p_done` → `asr_transcript_emitted` with `user_transcript` populated → `addressing_classified` event with `user_addressed_agent=True` (wake-word tier) → `policy_decision` with `action_type=full_response` and `primary_reason_code=EOU_CONFIRMED` → `foreground_proposal` → `tts_audio_emitted` → Kokoro audio plays in the browser.

**C. Address the agent (MiniCPM tier).** Say "hey, can you summarize what I just read?" (no wake-word). Expected: same as B but `addressing_classified` cites the MiniCPM-derived primary path. If MiniCPM is unavailable, `signal_producer_fallback` fires and the wake-word/safety-net path takes over.

**D. Talk past the agent.** Say "ugh, this code is broken" (not addressed). Expected: `addressing_classified` returns `user_addressed_agent=False` → `policy_decision` shows `action_type=silence` with `NOT_ADDRESSED_TO_AGENT`.

**E. Thinking pause.** Say "I was thinking... [1.5 s silence] ...maybe we should." Expected: SmartTurn v3 observably suppresses mid-pause EOU — watch for a `vad_signal_suppressed_by_smart_turn` event during the pause window and no `policy_decision` until the continuation. Final EOU on the continuation fires `full_response` if addressed. (Finding 13 closed: fix in `_detector_fanout_task` vetoes VAD signal when SmartTurn p_continue > p_done.)

**F. Backchannel.** Say "mm-hmm" or "yeah". Expected: `backchannel_classification` with a non-zero `p_backchannel`. The `emit_threshold=0.3` gate keeps noise out. Policy treats it as non-interrupting acknowledgement.

**G. Privacy mode `guest_present`.** Toggle the privacy mode in the dashboard. Say "my favorite color is teal." Expected: a `memory_commit_skipped` row with `CommitResult.SKIPPED_PRIVACY` cited. SleepTimeAgent batches but doesn't commit.

**H. Forget command.** Say "Claude, forget that." Expected: `explicit_forget` event fires. Tombstone persistence is wired via SleepTimeAgent path.

**I. "Why did you say that?"** Click any `policy_decision` row. Expected: the row opens its DecisionTrace blob — `PolicyInputs` snapshot, proposal candidates, gate evaluations, `retrieval_used` field (populated from real ASR transcript per v0.1j Task 11).

**J. Tool call.** Ask something that routes to a tool (the FastToolDispatcher fakes the tool surface in this rig; see `companion_harness/fast_tool_dispatcher.py` for the local-tool registry). Expected: `tool_call_requested → tool_call_dispatched → tool_progress_event* → tool_call_completed` chain; if you interrupt mid-tool by speaking, `tool_call_cancelled` fires within 300 ms.

### 2.5 Things to record

Per session, save:
- **Session ID** (printed in the startup banner; visible in the console).
- **Blob path** on disk (e.g., `/tmp/manual_test_blobs/<session_id>/`). Don't `rm` if you may want to replay.
- **One-line description** of what you tried and what surprised you.

Open one issue per surprising finding. Tag `manual-test-finding`. Don't bundle multiple findings.

### 2.6 Memory hygiene

Each session creates per-session memory dirs under `<blob_dir>/<session_id>/memory/{session,core,episodic,semantic}/`. To start completely clean:

```bash
rm -rf /tmp/manual_test_blobs/*
```

Operator-managed; the server does not auto-clean between sessions.

### 2.7 Tearing down

```bash
ss -ltnp | grep :8800     # find the PID
kill <PID>                # graceful
```

If unresponsive: `kill -9 <PID>`.

### 2.8 Tuning dashboard (right column)

Live Tier-B threshold tuning, served at the same `http://localhost:8800/`. Every change emits an auditable `config_change` + `operator_action` event pair — the dashboard is a first-class audit surface.

#### 2.8.1 Sections

The 12 keys group as:

| Section | Keys | Default expand |
|---|---|---|
| **Policy** (3) | `policy.backchannel_threshold`, `policy.audio_visual_conflict_threshold`, `policy.grounding_confidence_threshold` | expanded |
| **Detectors** (5) | `detectors.vad.speech_threshold`, `detectors.vad.silence_onset_ms`, `detectors.smart_turn.silence_onset_ms`, `detectors.smart_turn.silence_rms_threshold`, `detectors.backchannel.emit_threshold` | expanded |
| **Orchestrator** (4) | `orchestrator.proposal_batch_window_ms`, `orchestrator.hard_cancel_after_ms`, `orchestrator.p_speech_thresh`, `orchestrator.p_backchannel_thresh` | collapsed |

Canonical key list + ranges: `manual_test_console/config_schema.py::ALLOWLIST`.

#### 2.8.2 Drag semantics

Drag thumb → readout updates locally → `POST /config/patch` fires on release. Visual states:
- **At default (gray)** — value matches schema default.
- **Modified (blue + bold + `(modified)` badge + active `[↺]`)** — diverges from default; not yet submitted.
- **Pending (`⏳` + pulsing knob)** — patch in flight.
- **Accepted** — server returned 200.
- **Rejected (red row-flash + toast 3 s + revert)** — patch refused.

Rejection routing:

| Reason | HTTP status | Toast |
|---|---|---|
| Tier-A key (spec-pinned) | 403 | `Tier A — key not tunable` |
| Unknown key | 403 | `Unknown key` |
| Out of `[min, max]` | 400 | `Out of range: [min, max]` |

**Rejected patches emit no events.** Only the HTTP status signals rejection.

#### 2.8.3 Reset

- **Per-slider `[↺]`** — revert one key to its default.
- **Per-section `[reset]`** — revert all keys in a section.
- **Global `[reset all]`** — `POST /config/reset` reverts every Tier-B key.

Already-default keys still emit one `operator_action` on reset (request is recorded) but no `config_change` (nothing changed).

#### 2.8.4 Propagation timing

- **Policy thresholds** apply within the **current EOU decision** (no-await snapshot at EOU time).
- **Detector thresholds** apply on the **next frame** (snapshot once per frame).
- **Orchestrator timing** applies at the **next batch boundary** (read at start of proposal-batch window).

---

## 3. Phase 2 — Audio + video walkthrough

### 3.1 What changes

- Capture page requests both `audio: true` and `video: true`.
- New video panel renders thumbnail strip — last N frames with `event_id` + `payload_hash`.
- `raw_video_frame` events appear in left panel.
- **`--enable-vision` OFF** (default): frames captured + `raw_video_frame` events emit, but no `vision_frame` follow-ups; foreground receives `video=None`.
- **`--enable-vision` ON**: right panel gains `vision_frame` events (one per frame, `caused_by=[raw_video_frame.event_id]`). MiniCPM-o vision tower runs once per buffered frame (consume-once). Scene/AV-conflict/deictic/grounding still stub-return 0.0/False until #166/#168/#169/#172 PRs merge.

### 3.2 Start command (vision-on)

```bash
HF_HUB_CACHE=/raid/huggingface/hub \
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs --enable-vision
```

Banner reports `Vision: ENABLED (init_vision=True, +~18 GB VRAM)`. `/healthz` reports `vision_enabled`, `last_frame_event_id`, `frames_buffered`.

### 3.3 Scenarios

**K. Deictic without visual context.** Speak only (no camera grant). Say "what is this?" Expected: Phase 1 flow; `deictic_reference=False`.

**L. Deictic with visual context.** Grant camera, hold up an object, say "what is this?" Expected: `vision_frame` row arrives; foreground proposal may carry deictic reference (when #169 lands real impl).

**M. Audio-visual conflict.** Show camera one thing while saying another ("look at this red apple" while holding a green one). Expected (post-#168 merge): `audio_visual_conflict_score` rises; if it crosses `0.7`, SpeakDecision flips to `action_type=clarification` with `primary_reason_code=AUDIO_VISUAL_CONFLICT`. Today: stub returns 0.0.

**N. `no_camera_memory` privacy gate.** Toggle privacy to `no_camera_memory`. Hold up something memorable. Expected: visual content does not enter memory; `memory_commit_skipped` with `CommitResult.SKIPPED_CAMERA` fires OR no `memory_write_candidate` for visual content.

### 3.4 Known limitations

- Frame rate intentionally low (~1 fps) — manual-test rig, not a production camera path.
- No "what the agent thinks it sees" overlay.
- `--enable-vision` is +18 GB VRAM on top of the audio-only baseline (~28 GB). Total ~46 GB on b200.

---

## 4. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Every utterance comes back as `silence / NOT_ADDRESSED_TO_AGENT` | Wake-word path needs literal "Claude" or "Claudia". MiniCPM addressing path may be returning No. | Try wake-word scenario (B). Check `addressing_classified` event payload + `signal_producer_fallback` for the reason. |
| `backchannel_classification` rows with `p_backchannel=0` | Shouldn't happen — `emit_threshold=0.3` should suppress noise. | If you see them, check `detectors.backchannel.emit_threshold` in the dashboard; file a bug if at default. |
| Browser plays nothing on `full_response` | Check `/healthz` for `audio_out_chunks_sent`. If 0, no chunks left the harness. If >0, browser autoplay block likely. | Reload, click somewhere on the page first, retry. |
| Page loads but no events appear | WebSocket didn't connect; check browser devtools Network → WS | Re-grant mic permission; check SSH tunnel if used. |
| `vad_turn_signal` never fires | Mic gain too low; or silence too long | Verify mic input in browser; speak louder/closer; check `detectors.vad.speech_threshold`. |
| Lots of `log_drop_or_degrade` events | EventLogger backpressure | Save session; file a bug. The drain-storm bug from earlier sessions was fixed in PR #134. |
| Orphan count > 0 in trace view | Invariant #1 violation | Save session; file as bug with the orphan event_ids. |
| Server banner shows a "stub" label for any model | Deployed commit predates the relevant real-model PR | `git pull` and restart. |
| Slider rejects with red flash / `Tier A` toast | Key is Tier-A (spec-pinned, not tunable at runtime) | Make it tunable would require schema migration + new ALLOWLIST entry. See `design-config-and-dashboard.md` §1. |
| `vision_frame` events absent with `--enable-vision` ON | Camera not granted, or `_pending_frame` cleared by privacy gate transition | Check browser camera permission; check `/healthz` for `frames_buffered`. |

If you see something not listed: save session ID + blob path + browser console log; file an issue with all three.

---

## 5. What this rig is and isn't

**It is:** a low-fidelity, low-stakes observability surface so the lead can experience the harness running against live input and see the events the spec says should appear.

**It is not:**
- A demo (technical UI, not designed for non-leads).
- A latency benchmark (network jitter dominates; latency measured in replay tests).
- A test substitute (contract tests still gate; a passing session is informational, a failing one is evidence of a bug).
- A spec milestone (parallel to spec milestones; doesn't move any gate; invariants still bind).

---

## 6. Appendix — exact commands

```bash
# Start the server (audio-only, default)
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs

# Start with vision enabled (+18 GB VRAM)
HF_HUB_CACHE=/raid/huggingface/hub \
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs --enable-vision

# Start with native MiniCPM TTS instead of Kokoro
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs --tts-adapter native_minicpm

# CPU-only stubs (no MiniCPM, no Silero — fastest startup)
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs --use-stubs

# Check it's running
ss -ltnp | grep :8800        # Linux
lsof -i :8800                 # macOS / portable

# Local SSH port-forward (if laptop)
ssh -L 8800:localhost:8800 b200

# Open in browser
xdg-open http://localhost:8800/   # Linux
open http://localhost:8800/        # macOS

# Stop the server
kill <PID>      # see ss/lsof above
```

Eval CLI (separate from console):

```bash
pip install -e .[eval]      # optional extra
python -m companion_harness.evals run \
    --adapter harness_native \
    --output reports/
```

---

## 7. Where this fits

- [`architecture-v0.1.md`](architecture-v0.1.md) — frozen spec.
- [`model-stack.md`](model-stack.md) — every model + Protocol seam in one table; audio flow figure.
- [`project-progress-2026-05-15.md`](project-progress-2026-05-15.md) — current state of all milestones + in-flight work; intended for session handoff.
- [`eval-quickstart.md`](eval-quickstart.md) — eval CLI + output tree.
- [`eval-subsystem-spec.md`](eval-subsystem-spec.md) — eval design (Phases A/A.5/B1/B2/C/D + reporters).
- [`remote-dev.md`](remote-dev.md) — local↔b200 workflow.
- [`design-config-and-dashboard.md`](design-config-and-dashboard.md) — Tier A/B/C config + dashboard design.
- [`plan-vision-sidecar-wiring.md`](plan-vision-sidecar-wiring.md) — vision sidecar plan (v0.1h Task 2 implemented).
- [`plan-memory-wiring-followup.md`](plan-memory-wiring-followup.md) — 4-store memory wiring.

**Roadmap docs (chronological):** `roadmap-v0.1[a–j]-draft.md` — each milestone's locked anchors + OQ resolutions + tasks.

---

## 8. Default-on adapter history (v0.2e)

The following 6 opt-in adapters were flipped to **default-ON** in v0.2e (2026-05-16). Each had previously required an explicit `--enable-X` flag; use `--no-enable-X` to revert to the null-stub posture.

| Adapter | CLI flag | Default before v0.2e | Default since v0.2e | Rollback |
|---|---|---|---|---|
| `CLIPSceneChangeScorer` | `--enable-clip-scene` | OFF | **ON** | `--no-enable-clip-scene` |
| `HeuristicAVConflictScorer` | `--enable-av-conflict` | OFF | **ON** | `--no-enable-av-conflict` |
| `MiniCPMDeicticDetector` | `--enable-deictic` | OFF | **ON** | `--no-enable-deictic` |
| `ProsodyLexiconUrgencyScorer` | `--enable-urgency` | OFF | **ON** | `--no-enable-urgency` |
| `GroundingDINOAdapter` | `--enable-grounding` | OFF | **ON** | `--no-enable-grounding` |
| `SentenceTransformerEmbedder` | `--enable-embeddings` | OFF | **ON** | `--no-enable-embeddings` |

Timing instrumentation (Stage-2 escape valve per `docs/plan-v0.2e-execution.md §profiling rig`):
- `scene_change_score_ms` — stored on `VisionSidecar._last_scene_change_score_ms`; accessible via `last_scene_change_score_ms()`.
- `grounding_confidence_ms` — emitted in `deictic_grounding` event `payload_inline`.
- `audio_visual_conflict_ms` — stored on `VisionSidecar._last_av_conflict_ms`.
- `urgency_score_ms`, `deictic_reference_ms`, `embedding_ms` — wall-clock at call site (b200 profiling uses `time.monotonic()` around the respective `.score()` / `.classify()` / `.embed()` calls).

Deferred to v0.3: `AttachmentRiskMonitor` default-on posture (OQ-3), `PyannoteDiarizationAdapter` (depends on v0.2b).
