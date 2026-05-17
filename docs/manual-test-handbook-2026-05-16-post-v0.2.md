# Manual-test handbook — 2026-05-16 post-v0.2

This handbook supersedes the prior `docs/manual-test-handbook.md` for any test session
targeting the post-v0.2 codebase (HEAD `e0fc45c` or later on `main`).

---

## §1 — Briefing for the test-assistant

```
You are the test-assistant for the post-v0.2 working session.

Your job: help the operator manually validate features added since the v0.2 tag
(~28 PRs merged; see docs/progress-2026-05-16-post-v0.2.md for the full list).

Console state: live at http://localhost:8800/
HEAD: e0fc45c (or whatever HEAD main is at test time — confirm with curl /healthz)
POLICY_VERSION: v0.2-final (unchanged since v0.2 tag)

Default configuration at boot:
  vad    ENABLED  (Silero ONNX)
  asr    ENABLED  (faster-whisper-tiny, cuda, float16)
  tts    ENABLED  (Kokoro-82M-ONNX)
  smart_turn            DISABLED
  backchannel           DISABLED
  scene_scorer          DISABLED
  grounding_model       DISABLED
  av_conflict_scorer    DISABLED
  urgency_scorer        DISABLED
  embedder              DISABLED
  attachment_risk_monitor DISABLED
  fast_tool_dispatcher  DISABLED

Foreground model: MiniCPM-o (always loaded, not a hot seam).
Deictic: wired via --enable-deictic (default on); reads /healthz deictic_model field.

This is the basic stack: vad + asr + tts only. The 9 other hot seams are
default-OFF as of PR #329. You do NOT need to toggle anything to reach the
tested configuration — the server boots into it.
```

Do not dispatch coders unless the operator explicitly asks. Your job is observation,
capture, and aggregation.

---

## §2 — Test surfaces to exercise

For each surface: what it is, how to probe, expected result.

---

### 2.1 — Live conversational chat

**What it is:** the headline end-to-end audio path (PR #316 wired the MiniCPM addressing
classifier; PR #306 fixed orchestrator races; PR #308 fixed proposal grace window).

**How to probe:** speak via the browser mic at `http://localhost:8800/`. Say something
directly addressed to the agent (e.g., "hey, what is two plus two?").

**Expected event chain in the audit panel:**

```
raw_audio_chunk
  → vad_frame × N
  → vad_turn_signal (p_done ≥ 0.5)
  → asr_transcript_emitted (user_transcript populated)
  → addressing_classified
      classifier_name = MiniCPMAddressingClassifierImpl
      user_addressed_agent = true
  → policy_decision
      action_type = full_response
      primary_reason_code = EOU_CONFIRMED
  → foreground_proposal
  → tts_synthesis_started
  → assistant_audio_buffer_queued × N   (≥1 chunk)
  → tts_synthesis_completed
```

**Expected audio:** synthesized voice plays in the browser after `tts_synthesis_started`.

**Failure indicators:**
- `classifier_name = WakeWordAddressingClassifier` on every turn → PR #316 wiring not in
  effect; restart the server.
- `primary_reason_code = MISSING_SIGNAL_PRODUCER` → ASR seam is off; re-enable via dashboard
  Hot Seams toggle or restart.
- `synthesis_skipped_no_proposal` → proposal grace window expired; check GPU load
  (`nvidia-smi`) and widen via `POST /config/patch` if needed (max 1500 ms, PR #308).

---

### 2.2 — Sub-chunked TTS output (PR #324)

**What it is:** Kokoro PCM output is now sliced into ≤200 ms chunks (≤9600 bytes at
24 kHz mono 16-bit) before being forwarded to `/ws/audio_out`. This unblocks the
barge-in predicate window and is a prerequisite for Path B.

**How to probe:** speak a multi-sentence utterance. Capture `/ws/audio_out` message
sizes for 10 s (any WS client works; `tests/manual/repro_f0a_f0b.py auto` records them
in `audio_out.jsonl`). Alternatively inspect the `assistant_audio_buffer_queued` events
in the audit panel — each event's `payload_inline.byte_count` should be ≤ 9600.

**Expected:** every chunk ≤ 9600 bytes (≤ 200 ms PCM at 24 kHz mono 16-bit).

---

### 2.3 — /metrics Prometheus endpoint (PR #302)

**How to probe:**

```bash
curl -s http://localhost:8800/metrics | head -30
```

**Expected:** `Content-Type: text/plain; version=0.0.4; charset=utf-8` response body
containing at minimum:

```
harness_uptime_seconds ...
harness_adapter_ready{adapter="vad"} 1
harness_adapter_ready{adapter="asr"} 1
harness_adapter_ready{adapter="tts"} 1
harness_adapter_ready{adapter="smart_turn"} 0
```

**Failure indicator:** HTTP 404 → route not mounted; file a bug.

---

### 2.4 — /healthz blob_rotation_alive (PR #308)

**How to probe:**

```bash
curl -s http://localhost:8800/healthz | python3 -c "import sys,json; d=json.load(sys.stdin); print('blob_rotation_alive:', d.get('blob_rotation_alive'))"
```

**Expected:** `blob_rotation_alive: True`.

---

### 2.5 — Audit chain closure on drops (PR #325)

**What it is:** before PR #325, `_drop_oldest_put` used the literal string
`"_dropped_before_enqueue"` as the `caused_by` event ID on drop events, producing
a fake ID that resolves to nothing. PR #325 fixed this so drops carry a real
predecessor event ID.

**How to probe:** run the auto repro tool for 30+ seconds of synthetic audio:

```bash
python3 tests/manual/repro_f0a_f0b.py auto --inter-utterance-ms 500 \
    --out-dir /tmp/repro_post_v0.2
```

Grep the output for the sentinel:

```bash
grep '"_dropped_before_enqueue"' /tmp/repro_post_v0.2/events.jsonl | wc -l
```

**Expected:** output is `0`. Any non-zero count means the fix is not in effect.

---

### 2.6 — Audio tee depth raised + summary event (PR #325)

**What it is:** tee queue depth raised from 64 → 256; `audio_tee_drop_summary` events
emitted every 60 s when drops occurred in that window.

**How to probe:** sustain a 70+ second session (keep speaking or leave audio running).
Filter the dashboard for `audio_tee_drop_summary` (add to the type filter chip row).

**Expected:** if any tee overflow happened during the session, at least one
`audio_tee_drop_summary` event appears with payload
`{drop_count_60s, tee_name, current_queue_depth}`. Total drop count should be
significantly lower than the pre-#325 baseline of 148–157 drops per 12 seconds.

---

### 2.7 — Logprob addressing classifier (PR #322)

**What it is:** binary addressing classification via token log-probabilities replaces
the fragile chat-parse path. Root fix for addressing quality on natural conversation.

**How to probe (confidence float):** speak any utterance and inspect the
`addressing_classified` event in the audit panel.

**Expected:** `payload_inline.confidence` is a float in `[0.0, 1.0]` (not absent, not
`null`, not a string).

**How to probe (low-confidence branch):** speak an ambiguous utterance such as
"um, kind of" or "I guess so".

**Expected:** if `confidence ∈ [0.45, 0.55]`, an `addressing_classifier_low_confidence`
event fires immediately after `addressing_classified`.

**Also confirm:** `classifier_name = MiniCPMAddressingClassifierImpl` (PR #316 wiring).
If this reads `WakeWordAddressingClassifier`, the foreground model was not threaded in.

---

### 2.8 — MISSING_SIGNAL_PRODUCER reason code (PR #301)

**What it is:** when the speak-policy fires without an addressing signal (e.g., because
ASR is disabled), the silence reason is now explicitly `MISSING_SIGNAL_PRODUCER` rather
than indistinguishable from `NOT_ADDRESSED_TO_AGENT`.

**How to probe:**

1. Open the dashboard Hot Seams drawer.
2. Toggle the `asr` seam OFF.
3. Speak.

**Expected:** `policy_decision` event with `action_type = silence` and
`primary_reason_code = missing_signal_producer` (case-insensitive match on the
enum value).

**Cleanup:** re-enable ASR via the same toggle before continuing other tests.

---

### 2.9 — Dashboard event filter UI (PRs #303, #313, #318)

**What it is:** PR #303 added the filter bar; PR #313 fixed the filter not actually
filtering (legacy direct-append deleted); PR #318 added the line cap.

**How to probe:**

1. Open `http://localhost:8800/` and let events accumulate for 10–20 seconds.
2. Find the filter bar at the top of the audit panel.
3. Uncheck `vad_frame` in the type-chip row.

**Expected:** rows with event type `vad_frame` disappear from the panel immediately.
New `vad_frame` events do NOT reappear while the checkbox is unchecked.

4. Confirm the counter reads `Showing N of M (cap: 500)`.
5. Change the cap selector from 500 to 100.

**Expected:** older rows trim; the panel shows at most 100 rows.

**Failure indicator:** vad_frame rows reappear after unchecking → legacy append not
deleted (PR #313 not in effect).

---

### 2.10 — Audit row payload preview (PR #310)

**What it is:** each event row now shows an event-type-aware preview string instead of
raw `ts/src/seq/caused_by/hash` metadata. Full metadata appears on hover.

**How to probe:** after any live audio turn, find an `addressing_classified` row in the
audit panel.

**Expected:**
- Row body shows something like `addressed=true (MiniCPMAddressingClassifierImpl)` —
  the type-aware preview, not raw metadata.
- Hovering over the row surfaces a tooltip containing `ts`, `src`, `seq`, `caused_by`,
  and `hash` fields.

Test a second event type: find a `policy_decision` row.

**Expected preview:** `action_type=full_response | reason=EOU_CONFIRMED` (or the actual
values for that decision).

---

### 2.11 — Pinned basic-stack defaults at fresh boot (PR #329)

**What it is:** `GET /config/seams` on a freshly started server now returns the tested
basic-stack configuration without any operator action.

**How to probe:**

```bash
curl -s http://localhost:8800/config/seams | python3 -m json.tool
```

**Expected:** a JSON object where `vad`, `asr`, and `tts` are `true`, and ALL other
seams (`smart_turn`, `backchannel`, `scene_scorer`, `grounding_model`, `av_conflict_scorer`,
`urgency_scorer`, `embedder`, `attachment_risk_monitor`, `fast_tool_dispatcher`) are `false`.

No operator toggle required — this is the boot state.

---

### 2.12 — Eval console (PRs #293, #312, #314)

**What it is:** PR #293 added the eval console; PR #312 fixed the event-log reader path;
PR #314 fixed `event_log_path` serializing as `"None"` string.

**How to probe:**

1. Navigate to `http://localhost:8800/eval`.
2. Select the `harness_native` synthetic adapter.
3. Click **Run** and wait for completion (< 10 s for the default synthetic cases).
4. Note the `run_id` from the UI or from `GET /eval/runs`.

```bash
# Substitute your run_id:
RUN_ID=<run_id>
CASE_ID=thinking_pause   # or whichever case was run

curl -s "http://localhost:8800/eval/runs/${RUN_ID}/event_logs/${CASE_ID}" | head -5
```

**Expected:**
- HTTP 200 with a JSONL body (not a 404).

Confirm `event_log_path` is JSON `null` (not the string `"None"`) for synthetic-only cases:

```bash
curl -s "http://localhost:8800/eval/runs/${RUN_ID}" | \
    python3 -c "import sys,json; cases=json.load(sys.stdin).get('cases',[]); \
    [print('FAIL string None' if c.get('event_log_path')=='None' else 'OK',c['case_id']) for c in cases]"
```

**Expected:** every line prints `OK`.

---

### 2.13 — Hot-seam toggles (PRs #285–#290)

**What it is:** the tuning drawer exposes all 12 hot seams as real ↔ disabled toggles
with a full audit trail.

**How to probe:**

1. Open the dashboard. Click the **Hot Seams** section in the right tuning drawer.
2. Toggle one seam (e.g., `smart_turn`) ON.
3. Watch the audit panel for the event chain.

**Expected:**

```
operator_action
  → model_swap_requested  {seam: "smart_turn", to_enabled: true}
  → model_swap_completed  {seam: "smart_turn", applied_at_ms: ..., latency_ms: ...}
```

All three events must appear; `caused_by` chain must close (no orphan IDs).

4. Toggle `smart_turn` back OFF. Confirm a second complete chain with `to_enabled: false`.

---

### 2.14 — Deictic real adapter (PR #307)

**How to probe:**

```bash
curl -s http://localhost:8800/healthz | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('deictic_model'))"
```

**Expected:** `real:MiniCPMDeicticDetector` (not `stub:_NullDeicticModel`).

---

### 2.15 — Display subscriber drop event (PR #321)

**What it is:** when a display subscriber's queue saturates, a `display_subscriber_drop`
event fires with `{subscriber_id, subscriber_drop_count, dropped_event_type, queue_depth}`.
Per-subscriber queue raised from 256 → 1024 (reduces saturation under normal load).

**How to probe:** cause sustained high event volume (long monologue, or run the auto repro
tool at a short inter-utterance interval):

```bash
python3 tests/manual/repro_f0a_f0b.py auto \
    --inter-utterance-ms 200 \
    --out-dir /tmp/repro_drop_test
```

**Expected:** if broker queues saturate, `display_subscriber_drop` events appear in the
capture. These are hidden by default in the dashboard type filter; enable the checkbox to
see them.

If no drops occur: note that in the findings as a positive — it means the queue-depth
increase (1024) is sufficient for the tested load.

---

## §3 — Known limitations (do not pursue; log if hit)

- **`--streaming-speculative` flag (Path B)** — the flag is wired (PR #327) but
  Path B core behavior (continuous proposer, commit/discard) is in PR #328 which may
  or may not be merged at handbook write time. If not merged, the flag is a no-op skeleton.
  Do not attempt to test Path B unless PR #328 is confirmed merged.

- **F0a end-to-end race** — code fix is in place (PR #306, `_decision_in_flight` reset
  in `finally:`). Driver-level verification under sustained load is pending (PR #323 added
  the tooling; the operator-schedule baseline sweep has not run yet). Single-turn tests pass.

- **Concurrent multi-session correctness under TTS load** — not verified. Single-tab tests
  pass. Do not open multiple tabs simultaneously unless testing that specifically.

- **TTS playback cancellation on barge-in** — PR #324 sub-chunking enables the barge-in
  predicate window; full integrated barge-in is Path B §3.4 (pending). Current barge-in
  behavior is best-effort only.

- **`--minicpm-streaming-raw` mode has no audio output** — pre-v0.2 known limitation,
  deferred. Do not test this mode.

- **G1 / VAD-frame phantom refs** — the MiniCPM-path `-nd-` phantom refs are fixed (PR #317).
  The VAD-frame variant (R2-3) is still open. Observable as unresolved `caused_by` IDs in
  the display fan-out for VAD-frame predecessors. Log if encountered; do not pursue as a
  separate finding unless the orphan count climbs above single digits.

---

## §4 — How to help the operator

1. Do not dispatch coders unless the operator explicitly asks.
2. When the operator reports a finding, capture:
   - Event log excerpt (paste the relevant JSONL lines or audit panel screenshot).
   - Expected vs. observed behavior.
   - Severity: **BLOCKER** (prevents core functionality) / **CONCERN** (ship-with-known-issue) / **NIT** (polish).
3. Aggregate findings into `docs/manual-test-findings-2026-05-16-round-4.md` — one bullet
   per issue, sorted by severity within section. Cross-reference findings from rounds 1–3
   (`docs/manual-test-findings-2026-05-16.md`, `-round2.md`) — do not re-litigate already-closed
   items.
4. If a finding duplicates a known limitation from §3, note that and do not re-file it as new.
5. At session end, produce a summary table: total findings by severity, total positively
   verified surfaces, any surfaces not exercised.

---

## §5 — Do NOT

- Modify production code (read-only test session).
- Spawn codex agents (disabled per operator preference; use plan-critic or reviewer subagent
  if analysis is needed).
- Run the baseline-capture latency sweep without operator approval — it requires taking
  the console offline for ~10 minutes.
- Tag or push to remote.
- Clear blob storage without operator confirmation (blobs may be needed for replay).

---

## §6 — Console restart procedure (if needed)

```bash
pkill -9 -f manual_test_console.server
sleep 5
# Sync to latest main:
cd /home/yid042/projects/companion-agent-harness
git fetch origin main && git reset --hard origin/main
# Start with vision enabled (standard for live sessions):
nohup /raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --port 8800 --enable-vision \
    > /tmp/console.log 2>&1 &
echo "PID: $!"
```

Wait ~30–60 seconds for MiniCPM-o to load. Verify:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8800/healthz
# Expect: 200
curl -s http://localhost:8800/healthz | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print({k:d[k] for k in ['vad_ready','asr_ready','tts_ready','minicpm_loaded']})"
```

**Expected on healthy boot:** all four fields `True`. Omit `--enable-vision` for audio-only
boot; all §2 tests still apply.

---

## §7 — Reference

Key HTTP surfaces: `/healthz`, `/metrics`, `/config/seams`, `POST /config/model-swap`,
`POST /config/patch`, `POST /config/reset`, `/eval`, `POST /eval/runs`,
`GET /eval/runs/{id}`, `GET /eval/runs/{id}/event_logs/{case_id}`.
WebSocket surfaces: `/ws/ingest`, `/ws/display`, `/ws/audio_out`.

Deeper reading:
- `docs/basic-stack-design-2026-05-16.md` — per-hop audio path, invariant contracts, QA plan.
- `docs/progress-2026-05-16-post-v0.2.md` — full PR list and open items.
- `docs/manual-test-findings-2026-05-16.md` and `-round2.md` — prior findings (rounds 1–3).
