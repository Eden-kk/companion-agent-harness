# Basic stack design — MiniCPM + VAD + ASR + TTS (2026-05-16)

Target: v0.2 + 13 fix PRs (#301–#317), HEAD `8dae451`. Console process on port 8800.

---

## 1. Pinned configuration

Hot-seam state managed via `POST /config/model-swap`. State readable at `GET /config/seams`.

| Seam | Enabled | Adapter |
|---|---|---|
| vad | YES | Silero ONNX (`SileroVADModel`) |
| asr | YES | whisper-tiny.en (`FasterWhisperASRModel`, cuda, float16) |
| tts | YES | Kokoro-82M-ONNX (`KokoroTtsAdapter`) |
| smart\_turn | NO | (off — VAD-only EOU path) |
| backchannel | NO | |
| vision | NO | omit `--enable-vision` from CLI |
| scene\_scorer | NO | |
| grounding\_model | NO | |
| av\_conflict\_scorer | NO | |
| urgency\_scorer | NO | |
| embedder | NO | |
| attachment\_risk\_monitor | NO | |
| fast\_tool\_dispatcher | NO | |

**Foreground model:** MiniCPM-o (`MiniCPMStreamingModel`) — always loaded, not a seam.
**Deictic:** governed by `--enable-deictic` CLI flag (default on as of PR #307, but requires foreground model; wired in `_on_startup_finalize_deictic`).

**Start command (basic stack):**
```
python -m manual_test_console.server --port 8800
```
No `--enable-vision`, no `--minicpm-streaming-raw`, no `--use-stubs`.

---

## 2. End-to-end audio path

```
Browser mic
    |  WebSocket /ws/ingest (aiohttp, max 4 MB msg)
    v
server.py _handle_ingest_ws           server.py:560-640
    | InputIngest.ingest_chunk()       input_ingest.py:131
    | emits raw_audio_chunk            [caused_by: prev_chunk or session_open]
    v
audio_in asyncio.Queue                 orchestrator (maxsize=64 frames per tee)
    |
    v  _audio_tee_task                 realtime_orchestrator.py:415
    |  fans to two queues (drop-oldest on overflow → log_drop_or_degrade)
    |
    +---------> _tee_to_detectors -----> T1 _detector_fanout_task  :427
    |               VADDetector.process_frame()     vad_silero.py
    |               emits vad_frame                [caused_by: chunk_event_id]
    |               on EOU: emits vad_turn_signal  [caused_by: vad_frame_event_id]
    |               → put (TurnSignal, signal_evt_id) on _t2_inbox
    |
    +---------> _tee_to_foreground ---> T3 _foreground_stream_task  :963
                    drains stale frames at batch open (F0b fix, :970-977)
                    _bounded_frame_gen() yields frames until _batch_close_event fires
                    feeds MiniCPMStreamingModel.process_stream()
                    on each token: proposal_buffer.append(proposal); _first_proposal_event.set()

    T2 _policy_gate_task               realtime_orchestrator.py:481
        receives (TurnSignal, signal_evt_id) from _t2_inbox
        coalescing guard: _decision_in_flight skips duplicate EOU signals
        calls ASRModel.transcribe(turn_audio_buffer) → emits asr_transcript_emitted
        calls MiniCPMAddressingClassifier (primary, PR #316) → emits addressing_classified
        calls WakeWordAddressingClassifier (safety-net) if primary unavailable
        calls SpeakPolicy.decide()     speak_policy.py
        emits policy_decision          [caused_by: addressing_classified_event_id]
            payload_inline: {action_type, primary_reason_code}
        opens batch for T3: _batch_open_event.set()
        puts (signal_evt_id, Future[SpeakDecision], policy_evt_id) on _policy_decisions

    T4 _synthesis_dispatch_task        realtime_orchestrator.py:1032
        awaits policy_decision Future
        closes batch: _batch_close_event.set()
        if silence → clear buffer, _decision_in_flight=False, continue
        grace window: wait up to proposal_batch_window_ms (default 600, max 1500) for first proposal
            timeout → emits synthesis_skipped_no_proposal with typed payload_inline (PR #315)
        snapshot proposal_buffer
        AudioOutputController.start_generation() → emits assistant_generation_start + tts_synthesis_started
        KokoroTtsAdapter.synthesize() → PCM16 mono 24 kHz chunks (1–3 s latency)
        AudioOutputController.play(chunks) → sink → AudioOutBroker → /ws/audio_out
        emits tts_synthesis_completed
        finally: _decision_in_flight=False  (F0a fix, PR #306)

    AudioOutBroker                     server.py:282
        fans synthesized audio chunks to all /ws/audio_out subscribers
        browser plays PCM via Web Audio API
```

### Per-hop detail

| Hop | File:line | Event emitted | Queue depth / drop policy | Latency budget |
|---|---|---|---|---|
| Browser → /ws/ingest | server.py:560 | — | aiohttp, 4 MB msg cap | network RTT |
| ingest_chunk | input\_ingest.py:131 | `raw_audio_chunk` | EventLogger ring 16384, async | <1 ms |
| audio\_in → tee | orchestrator.py:415 | `log_drop_or_degrade` on overflow | 64-frame ring per tee | <0.1 ms |
| T1 VAD frame | vad\_silero.py | `vad_frame` | sampled 1-in-N on display (not in ring) | ~2 ms/frame |
| T1 EOU signal | orchestrator.py:470 | `vad_turn_signal` | _t2_inbox queue | <0.1 ms |
| T2 ASR | asr\_faster\_whisper.py | `asr_transcript_emitted` | synchronous in T2, blocks gate | 200–500 ms |
| T2 addressing | addressing\_classifier.py | `addressing_classified` | synchronous | <100 ms |
| T2 policy | speak\_policy.py | `policy_decision` | synchronous | <1 ms |
| T3 proposal | foreground\_model\_minicpm.py | `foreground_proposal` | proposal\_buffer list | first token 200–600 ms |
| T4 synthesis | tts\_kokoro.py | `tts_synthesis_started` / `tts_synthesis_completed` | streaming chunks | 1–3 s Kokoro latency |
| AudioOutBroker → browser | server.py:282 | audio chunks on /ws/audio\_out | per-subscriber queue | <10 ms |

---

## 3. Quality contract per invariant

### Invariant 1 — No unlogged behavior

Every raw\_audio\_chunk, vad\_frame, vad\_turn\_signal, asr\_transcript\_emitted, addressing\_classified, policy\_decision, foreground\_proposal, tts\_synthesis\_started/completed, and audio output event carries all 9 required fields (`event_id`, `session_id`, `schema_version`, `seq_no`, `event_type`, `timestamp_mono_ms`, `timestamp_wall`, `source`, `caused_by`).

**Fix that cemented this:** PR #315 (F4) added typed `payload_inline` to `signal_producer_fallback` and `synthesis_skipped_no_proposal` — previously these carried `payload_inline: null`, violating the "recorded with causal predecessors" promise.

**Observable signal:** `log_drop_or_degrade` fires when the EventLogger ring (maxsize 16384, PR #305) fills. If this fires during a session, the ring is undersized for that load. The ring stores all events; display fan-out is sampled separately (PR #305, `display_sampling_rate=1` in test-safe default, 5 in prod).

**Residual gap (G1):** null-detector stubs synthesize event IDs (pattern `-nd-<seq>`) that downstream `caused_by` fields reference but the stubs never call `EventLogger.log()` for. PR #317 (G1) fixed the MiniCPM streaming model path; the VAD-frame phantom-ref variant (R2-3, exposed by PR #305 sampling) is still open. DAG closure observable on `/ws/display` is ~77% as of Round-2; the underlying audit ring is complete.

### Invariant 4 — No proactive speech without policy approval

Every TTS dispatch is gated on a `Future[SpeakDecision]` that `SpeakPolicy.decide()` must resolve to `action_type="full_response"` before `KokoroTtsAdapter.synthesize()` is called (T4, orchestrator.py:1041–1125). There is no path from EOU signal to audio output that bypasses this future.

**Fix that cemented this:** PR #306 (F0a) moved `_decision_in_flight = False` to the `finally:` block after `play_task` completion, so a second TTS dispatch cannot start while the first is still playing. The `coalesced_during_playback` event fires when a TurnSignal arrives during playback and is suppressed.

**Observable signal:** `coalesced_during_playback` (emitted by T2 when `_decision_in_flight=True` AND `_audio_output.is_playing`, orchestrator.py:553) shows how many turns were suppressed to enforce the single-voice contract.

### Invariant 8 — Silence wins ties

`SpeakPolicy.decide()` returns `action_type="silence"` for any turn where `user_addressed_agent` is `False`, where the `primary_reason_code` maps to a denial, or where no proposal arrives before `proposal_batch_window_ms` expires. The default is to not speak.

**Fix that cemented this:** PR #301 (F1) added `ReasonCode.MISSING_SIGNAL_PRODUCER` so silence caused by "no addressing signal" is surfaced in `policy_decision.payload_inline.primary_reason_code` rather than silently dropped. Before this PR, operators could not distinguish "addressed, denied" from "not addressed at all" from the event stream.

**Observable signal:** `policy_decision` with `action_type="silence"` and `primary_reason_code` in `{NOT_ADDRESSED_TO_AGENT, MISSING_SIGNAL_PRODUCER, LOW_URGENCY, RUBRIC_VIOLATION}`.

### Invariant 10 — EventLogger is async and non-blocking

`EventLogger.log()` (event\_logger.py:59) is a non-blocking enqueue. When the internal `asyncio.Queue` (maxsize=16384, server.py:1334) is full, it emits a `log_drop_or_degrade` event (with count) rather than blocking the caller. The drain task runs as a background asyncio task, not on the audio path.

**Fix that cemented this:** PR #305 (F0d) raised the ring from 4096 → 16384 and added `DisplayBroker` sampling for `raw_audio_chunk` and `vad_frame` events (high-rate frame signals are sampled 1-in-N before the display fan-out; the ring receives all of them). This cut `log_drop_or_degrade` from ~78/12 s to ~11–13/12 s on equivalent synthetic load.

**Observable signal:** `log_drop_or_degrade` event rate in `/ws/display`. Zero is the target under normal conversational load; non-zero means the ring or sink is undersized.

---

## 4. Known risks for the basic stack (post-fix)

| Risk | Severity | Mitigation | Status |
|---|---|---|---|
| F1 design: voice-mode addressing requires ASR | CONCERN | `MISSING_SIGNAL_PRODUCER` ReasonCode makes silence visible (PR #301); full fix needs wake-word-on-audio path or "address=unknown → allow" fallback | Design fix pending (v0.2.1) |
| F0a end-to-end race: duplicate TTS during fast back-to-back turns | CONCERN | Code fix in place (PR #306, `_decision_in_flight` reset in finally); driver-level repro needs a successful TTS + fast second turn to verify | Re-verify once F0c resolves |
| G1/R2-3 phantom caused\_by refs (VAD-frame variant) | CONCERN | MiniCPM path fixed (PR #317); VAD phantom refs introduced by PR #305 display sampling still open; breaks DAG closure on `/ws/display` | Needs sampled-event tombstone or caused\_by rewrite |
| Kokoro synthesis latency 1–3 s | Accepted | Natural conversational cadence; barge-in via cancellation (orchestrator.py:1298) | Accepted |
| MiniCPM-o context drift across turns | Unknown | No per-turn evidence\_at instrumentation yet | Instrument as follow-up |
| `proposal_batch_window_ms` default 600 ms still times out under full-stack load | CONCERN | 9/10 approvals timed out in full-stack retest (F0c-followup); primary cause is F0b stale-frame queue churn, not window width; fix F0b first then re-measure | F0b code-fixed (PR #306); driver verify pending |

---

## 5. Quality assurance plan

### 5.1 Automated tests

Gate the basic stack before any merge touching the audio path:

```bash
# In the canonical venv:
/raid/yid042/venvs/companion-harness/bin/pytest \
    tests/test_manual_test_live_pipeline.py \
    tests/test_speak_policy.py \
    tests/test_event_logger*.py \
    tests/test_addressing_classified_event.py \
    -v
```

Expected: all pass, 0 failures. The `test_live_pipeline_emits_vad_and_policy_events` test in particular verifies the causal DAG closes (orphan count == 0) across a full EOU cycle.

Full suite readiness gate:
```bash
python scripts/v0_2_replay_report.py
```
Must read `READY FOR git tag v0.2` with 19/19 gates MET. As of HEAD `8dae451`, the banner passes with `POLICY_VERSION = v0.2-final`.

### 5.2 Manual repro

```bash
python tests/manual/repro_f0a_f0b.py auto \
    --inter-utterance-ms 800 \
    --out-dir /tmp/repro_basic_stack
```

What to verify in `/tmp/repro_basic_stack/events.jsonl`:
- Zero events with `event_id` in the `-nd-` phantom format as unresolved `caused_by` refs (G1 check for the basic-stack path)
- Zero `synthesis_skipped_no_proposal` events (F0c — proposer meets the grace window)
- Zero `coalesced_during_playback` on single-turn inputs (F0a — no stacked TTS)
- Every `policy_decision` carries non-null `payload_inline.primary_reason_code`
- `log_drop_or_degrade` count < 5 for a 2-utterance run (F0d headroom)

### 5.3 b200 gates

```bash
/raid/yid042/venvs/companion-harness/bin/pytest -m gpu
```
20/20 GPU tests must pass. Includes real Kokoro, real Silero, real FasterWhisper, and real MiniCPM-o inference paths.

### 5.4 Readiness banner

```bash
python scripts/v0_2_replay_report.py
```
Must print `READY FOR git tag v0.2` with 19/19 gates MET.

---

## 6. Operator runbook for the basic stack

### Start

```bash
python -m manual_test_console.server --port 8800
```

Wait for these lines in stderr (in order):
```
Loading MiniCPM-o foreground model (this may take minutes)...
MiniCPM-o loaded in X.Xs
Loading Kokoro-82M-ONNX TTS adapter...
Kokoro-82M-ONNX loaded in X.XXs
```
Then confirm startup:
```bash
curl -s http://localhost:8800/healthz | python3 -m json.tool
```
Expect `vad_ready: true`, `asr_ready: true`, `tts_ready: true`, `minicpm_loaded: true`.

### Sanity check

```bash
curl -s http://localhost:8800/metrics | grep harness_adapter_ready
```
Expect `harness_adapter_ready{adapter="vad"} 1`, `asr=1`, `tts=1`, `smart_turn=0`.

Speak via browser at `http://localhost:8800/`. In the audit panel observe this chain per turn:
```
raw_audio_chunk → vad_frame → vad_turn_signal
  → asr_transcript_emitted
  → addressing_classified (classifier_name=MiniCPMAddressingClassifierImpl)
  → policy_decision (action_type=full_response, primary_reason_code=EOU_CONFIRMED)
  → foreground_proposal
  → tts_synthesis_started
  → assistant_audio_buffer_queued × N
  → tts_synthesis_completed
```
Audio plays in browser after `tts_synthesis_started`.

### Recover from F1 silence (`MISSING_SIGNAL_PRODUCER`)

If `policy_decision` shows `primary_reason_code=MISSING_SIGNAL_PRODUCER`:
- Means the addressing classifier had no transcript input.
- Likely cause: ASR seam disabled via dashboard toggle.
- Fix: re-enable ASR via dashboard Hot Seams toggle, OR restart with the default CLI (ASR is on by default).

If `policy_decision` shows `primary_reason_code=NOT_ADDRESSED_TO_AGENT` for every turn during a normal conversation:
- Check `addressing_classified.classifier_name` — should be `MiniCPMAddressingClassifierImpl`. If it shows `WakeWordAddressingClassifier`, the foreground model was not wired (see F1b, fixed in PR #316).
- Restart the server; the model is threaded in at startup.

### Recover from F0c silence (`synthesis_skipped_no_proposal`)

If `synthesis_skipped_no_proposal` fires (policy approved but no TTS):
- The proposer did not emit a first token within `proposal_batch_window_ms` (default 600 ms).
- Check GPU load: `nvidia-smi`. If utilization > 90%, competing adapters are starving MiniCPM.
- Temporarily widen the grace window:
  ```bash
  curl -s -X POST http://localhost:8800/config/patch \
       -H 'Content-Type: application/json' \
       -d '{"key":"orchestrator.proposal_batch_window_ms","value":1500}'
  ```
  Max is 1500 (schema cap, PR #308).
- Root-cause fix is resolving any stale-frame churn in the foreground queue (F0b, code-fixed in PR #306 but verify under your load).

### Tune for stability

- If `log_drop_or_degrade` fires at high rate: restart with `--event-log-maxsize 32768` (raises ring above the default 16384). Do not lower `display_sampling_rate` below 1 in production (test-safe default is 1; set 5 for high-load sessions).
- If `coalesced_during_playback` fires on single-turn inputs: the F0a race is triggering; file a repro with the `events.jsonl` from `repro_f0a_f0b.py`.

---

## 7. What this design explicitly does not cover

- Vision pipeline (MiniCPM-o vision tower, CLIP, GroundingDINO, AVConflict scorer, VisionSidecar) — enable via `--enable-vision`.
- `--minicpm-streaming-raw` mode — bypasses SpeakPolicy and the audit gate; not production-ready.
- Eval console (`/eval`) and eval adapter infrastructure.
- Dashboard P2 features (fold/collapse filter bar, per-row pin column, `display_event_sampled` preview).
- Background reasoner (MCP tool calls, smart-path routing).
- Diarization adapter (pyannote) and speaker-separation signals.
- Embedder, urgency scorer, attachment risk monitor seams.
- b200-specific model weight paths and GPU memory planning.
- Replay report (`scripts/v0_2_replay_report.py`) internals.

---

## 8. Next milestones

### v0.2.1
- F2 line-cap: add retained-line cap (~500 lines) to audit panel DOM to prevent browser memory growth.
- G1/R2-3: emit sampled-event tombstone so display-fan-out DAG closes for downstream consumers.
- F0a driver verification: run `repro_f0a_f0b.py auto --inter-utterance-ms 200` and confirm zero `coalesced_during_playback` on a session with confirmed TTS playback.

### v0.3
- F1 voice-mode addressing design: wake-word-on-audio path OR `address=unknown → allow` fallback so the basic stack remains functional when ASR is disabled.
- Deictic upgrade: real `MiniCPMDeicticDetector` integration test under load.
- Scene scorer real adapter measurement: CLIP + GroundingDINO first-token impact on MiniCPM-o latency.
- MiniCPM-o context drift instrumentation: `evidence_at` per turn so the proposer's conditioning can be audited.
