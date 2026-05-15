# Audio-path design plan — v0.1f

> Working design + audit doc for the live audio path. Status: **DRAFT** — initial audit pass 2026-05-15, post-PR-#142 (`784990b`).
>
> Cites file:line against the post-#142 working tree. Cites spec line numbers in `docs/architecture-v0.1.md`. Spec is frozen at v0.1 — amendments are recorded as issues, not edits to the spec file.

---

## §1 — Audio→response logic, end-to-end

A narrative walk-through of every component an audio frame traverses from the browser microphone to either silence or a synthesized voice response. Numeric values cited inline; gate predicates trace to invariants in `CLAUDE.md` Part 2.

1. **Browser microphone capture.** `getUserMedia({audio: {channelCount: 1, sampleRate: 16000, echoCancellation: true, noiseSuppression: true}})` (`manual_test_console/index.html:380`). An AudioWorklet processor (`PCMChunker`) batches ~100 ms of Float32 audio (`manual_test_console/index.html:296-326`) and posts each batch to the main thread. Main thread downsamples to 16 kHz if needed (`index.html:328-338`), converts to little-endian int16 PCM (`index.html:340-348`), and sends as base64 JSON over WebSocket to `/ws/ingest` (`index.html:83`). Honors invariant #1: every chunk gets a wall + monotonic timestamp on the server side.

2. **Server ingest.** `_handle_ingest_ws` accepts each envelope, decodes the base64 PCM, builds a `CaptureMetadata`, and calls `ingest.ingest_chunk(session, payload_bytes, meta)` (`manual_test_console/server.py:316-325`). `InputIngest.ingest_chunk` writes a blob at `<blob_dir>/<event_id>`, computes payload hash, and emits a `raw_audio_chunk` `Event` with `payload_kind="raw_audio"`, `sensitivity="sensitive"`, `retention_policy_id="raw_media_default_300s"`, `caused_by=[prev_audio_chunk_id or session_open_id]` (`companion_harness/input_ingest.py:131-161`). Honors invariant #1 (timestamped, source-attributed, recorded with causal predecessor).

3. **Push to orchestrator queue.** When the live pipeline is enabled, `pipeline.push_audio(payload_bytes, evt.event_id)` enqueues `(frame_bytes, raw_audio_chunk.event_id)` onto `audio_in: asyncio.Queue[tuple[bytes, str]]` with `maxsize=64` (`manual_test_console/live_pipeline.py:280-297`, `:347`). Drop-oldest on overflow (`live_pipeline.py:286-297`) — honors invariant #10 (realtime path never blocks on log durability).

4. **Orchestrator fan-out (T0).** `_audio_tee_task` reads `audio_in`, fans each frame onto `tee_to_detectors` and `tee_to_foreground` (`companion_harness/realtime_orchestrator.py:315-325`). Also appends each frame to `self._turn_audio_buffer` when an ASR model is wired (`realtime_orchestrator.py:324-325`).

5. **Per-frame detectors (T1).** `_detector_fanout_task` (`realtime_orchestrator.py:327-345`) sends each frame to three detectors in turn:
   - **VAD (Silero VAD ONNX).** `VADDetector.process_frame()` calls the injected model (`turn_detector_vad.py:75-109`), emits `vad_frame` for every frame, and emits a `vad_turn_signal` + returns a `TurnSignal(detector="vad", ...)` when end-of-utterance is detected (`turn_detector_vad.py:87-107`). Thresholds: `speech_threshold=0.5`, `silence_onset_ms=300`, `frame_duration_ms=32` (`turn_detector_vad.py:26-27, 372-375`). Honors invariant #1 (`vad_frame` Event per frame).
   - **SmartTurn v3 (Pipecat).** `SmartTurnDetector.process_frame()` accumulates the turn audio internally and invokes the model only at silence-candidate moments (`turn_detector_smart.py:106-152`). Emits `smart_turn_invocation` + `smart_turn_signal` Events when fired (`turn_detector_smart.py:135, 145`). Settles `test_thinking_pause` (issue #10 amendment) and `test_backchannel_survival` resolution (issue #20 amendment requires both detectors).
   - **Backchannel classifier (whisper-tiny + lexicon).** `BackchannelClassifier.process_frame()` runs every frame but only emits a `backchannel_classification` Event and returns a `TurnSignal(detector="backchannel", p_backchannel=...)` when `p_backchannel >= emit_threshold` (default 0.3) (`backchannel_classifier.py:98-117`, `:89`). The emit-threshold was introduced by PR #134 to fix Finding 3 (drain saturation during silence).

   Each non-None `TurnSignal` is enqueued onto `_t2_inbox` (`realtime_orchestrator.py:337`). Every VAD frame also enqueues a `_VadOnsetFrame` for barge-in detection (`realtime_orchestrator.py:340-345`).

6. **Policy gate (T2).** `_policy_gate_task` reads `_t2_inbox` (`realtime_orchestrator.py:347-569`). Two paths:
   - **VAD onset frame.** If `_should_emit_speech_onset()` (`realtime_orchestrator.py:701-706`) — `p_speech > 0.5` AND `audio_output.is_playing` AND not in debounce — emit a `vad_user_speech_onset` Event and, if barge-in trigger conditions hold (`realtime_orchestrator.py:708-713`: playback active, no in-flight barge-in, recent `p_backchannel < 0.7`), spawn `_fire_barge_in` (`realtime_orchestrator.py:715-754`). `_fire_barge_in` calls `audio_output.request_stop()` then hard-cancels via `cancel_generation()` if play_task does not finish within `hard_cancel_after_ms = 120` (`realtime_orchestrator.py:200, 735-744`).
   - **TurnSignal.** Coalescing guard skips if a decision_future is still pending (`realtime_orchestrator.py:382-385`). Sorts `signal_history` lexicographically by `evidence_event_ids[0]` (`realtime_orchestrator.py:389-393`) — the **determinism boundary** referenced by invariant #5. Builds `PolicyInputs` via the injected `policy_inputs_builder` (`realtime_orchestrator.py:395`).

7. **PolicyInputs build (live).** `_live_policy_inputs_builder(signal, signal_history)` in `manual_test_console/live_pipeline.py:211-256`:
   - `social_mode = "user_addressing_agent"` (spec-enumerated value, spec line 799), hardcoded for single-user manual-test (`live_pipeline.py:236`).
   - `user_addressed_agent = (social_mode == "user_addressing_agent")` — mechanical derivation from spec's first-class addressing signal (`live_pipeline.py:244`). Replaces PR #140 stopgap; spec is silent on per-utterance derivation (open issue #139).
   - `privacy_mode = "normal"`, `current_task_mode = "normal"`, `risk_mode = "normal"` — all spec-enumerated values (spec lines 797, 478-480, 801).
   - `user_speaking = max_p_done <= max_p_continue` (`live_pipeline.py:232-234`).
   - `eou_probability = max_p_done` from sorted signal history.
   - Visual fields all zeroed: `scene_change_score=0.0`, `deictic_reference=False`, `audio_visual_conflict_score=0.0`, `grounding_confidence=1.0`, `deictic_ambiguous=False` (`live_pipeline.py:242-256`). Vision sidecar is wired (PR #141) but scoring/grounding are stubs (Anchor 5 of `docs/plan-vision-sidecar-wiring.md`).
   - Budget/cooldown empty: `proactivity_budget_remaining={}`, `cooldown_state={}`, `attachment_risk_level=0.0`, `urgency_score=0.0`.

8. **ASR (synchronous on EOU).** Inside T2, after `policy_inputs_builder` returns: `transcript = self._asr_model(bytes(self._turn_audio_buffer))` (`realtime_orchestrator.py:403-407`). `FasterWhisperASRModel` (`companion_harness/asr_faster_whisper.py`) runs whisper-tiny.en with `temperature=0.0`, `beam_size=1`, `condition_on_previous_text=False`, `language="en"` for determinism (`asr_faster_whisper.py:42-61`). Result assigned to `inputs.user_transcript` (`realtime_orchestrator.py:408`). Buffer cleared immediately (`realtime_orchestrator.py:407`).

9. **Memory retrieval.** Retrieval runs against `episodic_store` and `semantic_store` (`realtime_orchestrator.py:410-446`). Both are passed in by the live-pipeline factory; today both are `None` (`live_pipeline.py:401-417` does not provide them), so `retrieved_items = []` and `stores_queried = []`. A `memory_retrieval_event` is still emitted with empty results (`realtime_orchestrator.py:420-443`) — invariant #1 maintained even on the empty path. Wiring plan is at `docs/plan-memory-wiring-followup.md`.

10. **Policy decision.** `self._speak_policy_fn(inputs, [signal_evt_id], signal.p_backchannel)` calls the pure `decide()` function (`speak_policy.py:33-184`). The function evaluates gates in fixed order: blocking social modes → alert threshold → user_speaking → eou_probability ≤ 0.5 → backchannel threshold (0.7) → audio-visual conflict (0.7) → grounding confidence (0.5) → deictic ambiguity → short_reaction → `user_addressed_agent` → aesthetic_reaction → silence fallthrough (`speak_policy.py:58-184`). Determinism guarantees enumerated at `speak_policy.py:40-50`.

11. **DecisionTrace + audit emission.** `_build_decision_trace()` constructs the trace with `threshold_path` reconstructed via `_threshold_path_for()` (`speak_policy.py:200-260`). Trace is written to disk via `DecisionTraceStore` (`realtime_orchestrator.py:484`). A `policy_decision` Event is emitted with `payload_ref=trace_uri` AND inline `payload_inline={action_type, primary_reason_code}` (`realtime_orchestrator.py:486-505`) — the inline fields were added by PR #133 to fix Finding 5 (live console couldn't surface verdicts). A second `decision_trace_emitted` Event carries the canonical content hash (`realtime_orchestrator.py:518-528`).

12. **Synthesis dispatch (T4).** `_synthesis_dispatch_task` awaits the `decision_future` (`realtime_orchestrator.py:630-687`). On `action_type == "silence"`: clears the proposal buffer and continues (`realtime_orchestrator.py:639-642`). On any other action: opens an 80-ms (default `proposal_batch_window_ms=80`; live pipeline overrides to 200) grace window for at least one `ThinkerProposal` (`realtime_orchestrator.py:644-655`); on timeout emits `synthesis_skipped_no_proposal` (`realtime_orchestrator.py:652`). Snapshot of proposals → `_best_proposal_text` (max-confidence) → `audio_output.start_generation(caused_by=[policy_evt_id])` returns a `gen_event_id` (`realtime_orchestrator.py:663`) → `tts_adapter.synthesize(text, decision.allowed_prosody_tags)` returns an `AsyncIterator[bytes]` (`realtime_orchestrator.py:664`) → `audio_output.play(chunks, gen_event_id)` runs as a play_task so barge-in can cancel it (`realtime_orchestrator.py:667-674`). Honors invariants #2, #4 (synthesis only fires downstream of a policy-approved SpeakDecision; structurally enforced).

13. **TTS synthesis (Kokoro-82M-ONNX).** `KokoroTtsAdapter.synthesize(text, prosody_tags)` calls `Kokoro.create_stream(text, voice="af_bella", speed=1.0, lang="en-us")` (`tts_kokoro.py:119-140`). Each yielded Float32 sample batch is clipped to [-1, 1] and converted to little-endian int16 PCM bytes (`tts_kokoro.py:137-140`). Output sample rate is 24000 Hz (`tts_kokoro.py:77`). Per the module docstring, `prosody_tags` are accepted for Protocol conformance and **ignored** (`tts_kokoro.py:42-44`). Determinism is model-level reproducible; chunk timing is asyncio-level non-deterministic (acceptable under invariant #5, which is policy-determinism only).

14. **Audio output controller.** `AudioOutputController.play()` (`audio_output_controller.py:131-150`) iterates the async generator. For each chunk: checks `_stop_event`; if set, emits `assistant_audio_stop_completed` and returns. Otherwise calls the injected `sink(chunk)` — in live mode this is `WebSocketAudioSink.__call__(chunk)` (`live_pipeline.py:201-203`), which invokes `AudioOutBroker.publish(session_id, seq, chunk)`. On stream completion, emits `assistant_audio_buffer_flushed`. Events emitted: `assistant_generation_start`, `assistant_audio_buffer_queued`, `assistant_audio_buffer_flushed`, `assistant_audio_stop_requested`, `assistant_audio_stop_completed`, `assistant_generation_cancel_requested` (`audio_output_controller.py:84, 91, 96, 105, 125, 141`). Every event carries the `gen_event_id` in `caused_by[]` (closes the DAG back to the upstream SpeakDecision).

15. **AudioOutBroker fan-out.** `AudioOutBroker.publish()` (`manual_test_console/server.py:190-214`) puts each chunk onto every connected `/ws/audio_out` listener's bounded queue (`_AUDIO_OUT_QUEUE_DEPTH = 256`, `server.py:63`) with drop-oldest on overflow (`server.py:200-214`). Envelope shape:
    ```json
    {"type": "audio_chunk", "session_id": "...", "seq": <int>,
     "pcm_bytes_b64": "<base64>", "sample_rate": 24000,
     "sample_format": "pcm_s16le"}
    ```
    (`server.py:192-199`).

16. **Browser playback.** `connectAudioOut()` (`index.html:258-281`) registers an `onmessage` handler that decodes each `audio_chunk`. `enqueuePcmChunk()` (`index.html:235-256`) decodes the base64 PCM, converts to Float32, builds an `AudioBuffer`, creates an `AudioBufferSourceNode`, and schedules it at `Math.max(nextPlaybackTime, ctx.currentTime)` (`index.html:250-252`). Chained playback preserves continuity.

17. **EventLogger discipline (cross-cutting).** Every Event from steps 2–14 above is emitted via `EventLogger.log()` — non-blocking `put_nowait` on a `maxsize=4096` queue (`event_logger.py:48-53`, `server.py:514`). A separate drain task processes the queue and forwards to subscribers (`event_logger.py:69-86`). On `QueueFull`, the drop count is buffered and a `log_drop_or_degrade` event is emitted after the next sink call (`event_logger.py:81-86`). Honors invariant #10 (the realtime path never waits on log durability).

18. **Causal closure.** `CausalGraph.find_orphans()` reconstructs the DAG offline (`causal_graph.py:33-74`). An event is an orphan iff any `caused_by` ref is neither a known `event_id` nor a known sentinel (`_dropped_before_enqueue`). `harness_init` and `log_drop_or_degrade` are valid roots. In a well-formed session, `find_orphans().orphan_count == 0` — gates `test_causal_graph_completeness` (spec lines 339-343).

### Invariant cross-reference

| Step | Invariant honored | How |
|---|---|---|
| 2, 5, 8, 11, 14 | #1 (no unlogged behavior) | Every step emits an Event with `caused_by[]` |
| 12, 13 | #2 (no direct Thinker speech) | TTS adapter only called from `_synthesis_dispatch_task` after `decide()` approves |
| 10, 12 | #4 (no proactive speech without policy approval) | Synthesis structurally downstream of approved SpeakDecision |
| 10, 11 | #5 (policy-layer deterministic replay) | `decide()` is pure; determinism boundary at sorted signal history (step 6) |
| 3, 4, 15, 17 | #10 (logger non-blocking) | All queues are bounded; drop-oldest + `log_drop_or_degrade` emit |

---

## §2 — Audio-output-relevant parameter audit

Status legend:
- **CORRECT** — spec-defined, code uses it as spec intends.
- **MISUSE** — spec-defined, code uses it differently than spec intends.
- **MISSING_FROM_CODE** — spec-defined, no code path uses it.
- **EXTRA_TO_CODE** — code has it, spec doesn't define/authorize it.
- **PARTIAL** — spec-defined, code uses it but not fully.

### §2.1 — `PolicyInputs` fields

| Parameter | Spec ref | Code site | Status | Notes |
|---|---|---|---|---|
| `user_speaking` | spec line 220 | `live_pipeline.py:234`, `speak_policy.py:82` | CORRECT | Derived from `max_p_done <= max_p_continue` over sorted history. |
| `eou_probability` | spec line 221 | `live_pipeline.py:240`, `speak_policy.py:86` | CORRECT | `max_p_done` over signal history; gated `> 0.5` in `decide()`. |
| `assistant_speaking` | spec line 222 | `live_pipeline.py:241` | PARTIAL | Hardcoded `False` in builder. Real value is available via `audio_output.is_playing` (`audio_output_controller.py:152-154`) but not threaded into `PolicyInputs`. Spec doesn't pin a derivation. |
| `scene_change_score` | spec line 223 | `live_pipeline.py:242` | MISSING_FROM_CODE | Hardcoded `0.0`. VisionSidecar scoring is a stub (`server.py:115-119` `_NullSceneScorer`). |
| `deictic_reference` | spec line 224 | `live_pipeline.py:243` | MISSING_FROM_CODE | Hardcoded `False`. `DeicticDetector` is stubbed (`companion_harness/deictic_detector.py`, see roadmap Task 5). Plan defers real model to follow-up (`docs/plan-vision-sidecar-wiring.md` Anchor 5). |
| `user_addressed_agent` | spec line 225 | `live_pipeline.py:244` | PARTIAL | Mechanically derived from `social_mode` (post-#142). Spec silent on per-utterance derivation; issue #139 tracks the open decision. |
| `urgency_score` | spec line 226 | `live_pipeline.py:245` | MISSING_FROM_CODE | Hardcoded `0.0`. Spec uses it as alert-threshold gate (`speak_policy.py:64-66`) but no derivation in live pipeline (no audio energy / visual signal / hot-word path). |
| `proactivity_budget_remaining` | spec line 227 | `live_pipeline.py:246` | MISSING_FROM_CODE | Hardcoded `{}`. Used by `short_reaction` and `aesthetic_reaction` branches (`speak_policy.py:133-145, 169`). v0.1f Anchor (`docs/roadmap-v0.1f-draft.md`) locked the alphabet as empty-set; v0.1g will populate. |
| `privacy_mode` | spec line 228, 797 | `live_pipeline.py:247` | PARTIAL | Hardcoded `"normal"`. Spec enumerates 7 values (`normal`, `no_memory`, `no_camera_memory`, `local_only`, `guest_present`, `child_present`, `sensitive_conversation`). No runtime mode-change channel in live pipeline. Issue #96 tracks adapter-routing validation. |
| `current_task_mode` | spec line 229 | `live_pipeline.py:248` | PARTIAL | Hardcoded `"normal"`. Spec lines 478-480 enumerate `cooking`, `crisis_emergency`, `creative_focus`, `normal`; lines 482-487 add `walking_outdoor`, `sleep_winddown`, `group_unaddressed`. No detector. |
| `social_mode` | spec line 230, 799 | `live_pipeline.py:236, 249` | PARTIAL | Hardcoded `"user_addressing_agent"`. Spec enumerates 4 values (line 799). `_BLOCKING_SOCIAL_MODES` (`speak_policy.py:22-26`) gates the non-addressing-agent values. No real detector for multi-party scenarios. |
| `risk_mode` | spec line 231 | `live_pipeline.py:250` | PARTIAL | Hardcoded `"normal"`. Spec line 801 enumerates 5 values. No detector. |
| `cooldown_state` | spec line 232 | `live_pipeline.py:251` | MISSING_FROM_CODE | Hardcoded `{}`. Aesthetic-reaction branch reads `cooldown_state.get("aesthetic_reaction", 0)` (`speak_policy.py:169`). No runtime decrement/increment in live pipeline. |
| `attachment_risk_level` | spec line 233 | `live_pipeline.py:252` | MISSING_FROM_CODE | Hardcoded `0.0`. Spec Part 7 lines 838-857 require an AttachmentRiskMonitor that fires only on high-confidence signals; v0.1g Anchor 2 names the schema. Not yet implemented. |
| `audio_visual_conflict_score` | spec line 156, 452-454 | `live_pipeline.py:253` | MISSING_FROM_CODE | Hardcoded `0.0`. `_AUDIO_VISUAL_CONFLICT_THRESHOLD = 0.7` (`speak_policy.py:29`) is checked but the signal source is the vision pipeline (`_NullSceneScorer` stub). |
| `grounding_confidence` | spec line 157 | `live_pipeline.py:254` | MISSING_FROM_CODE | Hardcoded `1.0` (i.e., always-confident, default suppresses `VISUAL_LOW_CONFIDENCE` silence). Real grounding via `VisionSidecar.resolve()` (`vision_sidecar.py:226-266`) is not wired into the live policy_inputs_builder. |
| `deictic_ambiguous` | spec line 158, 448-450 | `live_pipeline.py:255` | MISSING_FROM_CODE | Hardcoded `False`. Real ambiguity detection requires a grounding model with confidence over multiple candidates; not present in the live pipeline. |
| `aesthetic_novelty_score` | spec line 160 (schema), policy gate 166 | not set in `live_pipeline.py` | PARTIAL | Defaults to `0.0` from dataclass default; aesthetic-reaction branch never fires. Source TBD by ThinkerProposalGen — Stage 6 / v0.1g. |
| `quiet_mode_active` | spec line 159 | not set in `live_pipeline.py` | MISSING_FROM_CODE | Defaults `False`. Spec includes a `quiet mode` first-class command (invariant #7); no live wiring. |
| `short_response_appropriate` | spec line 161 | not set in `live_pipeline.py` | MISSING_FROM_CODE | Defaults `False`. Used by `short_reaction` branch (`speak_policy.py:133-145`). No derivation path. |
| `user_transcript` | not in spec Part 5 (v0.1f addition) | `realtime_orchestrator.py:408`, schema line 164 | EXTRA_TO_CODE | Added by PR #136 for ASR output. Spec is silent on this field; code uses it to drive `_detect_explicit_remember` (`realtime_orchestrator.py:81-94, 535`) and is the natural input for a future `user_addressed_agent` per-utterance refiner. |
| `retrieved_items` | spec line 162 (schema), spec lines 514-516 | `realtime_orchestrator.py:412-446` | PARTIAL | Schema present; `episodic_store=None`, `semantic_store=None` in the live pipeline factory (`live_pipeline.py:305-321` does not pass them). Plan converged at `docs/plan-memory-wiring-followup.md`; not yet implemented. |
| `tool_progress_evidence` | spec/v0.1f Anchor 4, schema line 163 | not set in `live_pipeline.py` | MISSING_FROM_CODE | v0.1f field. Stage 5 not yet active in live pipeline. |

### §2.2 — `TurnSignal` fields

| Parameter | Spec ref | Code site | Status | Notes |
|---|---|---|---|---|
| `detector` | spec line 212 | `vad`/`smart_turn`/`backchannel` populated by detectors | CORRECT | Each detector sets its own label (`turn_detector_vad.py:99`, `turn_detector_smart.py:138`, `backchannel_classifier.py:110`). |
| `p_done` | spec line 213 | populated by detectors | CORRECT | VAD: `1.0 - p_speech` at EOU (`turn_detector_vad.py:97`). SmartTurn: model output. Backchannel: `_P_DONE=0.05` (`backchannel_classifier.py:68`). |
| `p_continue` | spec line 214 | populated by detectors | CORRECT | VAD: `p_speech`. SmartTurn: model output. Backchannel: `_P_CONTINUE=0.10` (`backchannel_classifier.py:69`). |
| `p_backchannel` | spec line 215 | populated by `BackchannelClassifier` | CORRECT | Always 0.0 from VAD / SmartTurn; `BackchannelClassifier` is the sole writer (`backchannel_classifier.py:106-114`). |
| `confidence` | spec line 216 | populated by detectors | CORRECT | VAD: `p_done`. SmartTurn: `p_done`. Backchannel: `p_backchannel`. Spec doesn't pin a derivation. |
| `evidence_event_ids` | spec line 217 | populated by detectors | CORRECT | Always `[frame_evt.event_id]` or `[invocation_evt.event_id]`. Drives the determinism sort (`realtime_orchestrator.py:390-393`). |

### §2.3 — `SpeakDecision` fields

| Parameter | Spec ref | Code site | Status | Notes |
|---|---|---|---|---|
| `action_type` | spec lines 236-239 | `speak_policy.py:67, 92, 121, 135, 150, 172, 188` | CORRECT | Eight values enumerated by spec; all reachable in `decide()`. |
| `primary_reason_code` | spec line 240 | `speak_policy.py` throughout | CORRECT | `ReasonCode` enum from `reason_codes.py:87+`. Stage 3 gap audit (`reason_codes.py:6-50`) concluded no gaps. |
| `supporting_reason_codes` | spec line 241 | `speak_policy.py:152` (one site) | PARTIAL | Used by `full_response` branch only (cites `USER_ADDRESSED_AGENT`). Other branches pass `[]`. Spec doesn't mandate population beyond audit traceability. |
| `redacted_explanation` | spec line 242 | always `None` in `decide()` | PARTIAL | Spec defines it as "free-text; retention-governed". No producer writes it today. Honors retention discipline by default. |
| `caused_by` | spec line 243 | always `[signal_evt_id]` | CORRECT | Closes the DAG to the triggering `TurnSignal` event. |
| `budget_bucket` | spec line 244 | `speak_policy.py:74, 97, 110, 127, 141, 156, 177` | CORRECT | Populated per action type; `None` on silence. |
| `allowed_prosody_tags` | spec line 245 | always `[]` in `decide()` | PARTIAL | Defined in spec; no producer fills it. Passed through to `KokoroTtsAdapter.synthesize` (`realtime_orchestrator.py:664`) but Kokoro ignores tags entirely (`tts_kokoro.py:42-44`, `:124-126`). Downstream prosody honoring is deferred to a richer TTS backend (CosyVoice2 or MiniCPM-o native). |
| `max_duration_ms` | spec line 246 | always `None` in `decide()` | PARTIAL | Spec defines it; no producer fills it. No enforcement at synthesis time. |

### §2.4 — Thresholds and tuning constants

| Parameter | Default | Spec ref | Code site | Status | Notes |
|---|---|---|---|---|---|
| `_BACKCHANNEL_THRESHOLD` | 0.7 | spec lines 412-414 (test) | `speak_policy.py:28` | CORRECT | Decision threshold for `BACKCHANNEL_DETECTED`. Spec is silent on numeric value; this is a tuning constant. |
| `_AUDIO_VISUAL_CONFLICT_THRESHOLD` | 0.7 | spec line 452-454 | `speak_policy.py:29` | CORRECT (with caveat) | Threshold honored in `decide()`. But `audio_visual_conflict_score` is hardcoded `0.0` upstream → branch never fires in live. |
| `_GROUNDING_CONFIDENCE_THRESHOLD` | 0.5 | spec line 444-446 | `speak_policy.py:30` | CORRECT (with caveat) | Threshold honored. But `grounding_confidence` hardcoded `1.0` upstream → branch never fires in live. |
| `_BLOCKING_SOCIAL_MODES` | `{user_addressing_other, group_conversation, background_presence}` | spec line 799 | `speak_policy.py:22-26` | CORRECT | Three of four spec values cause silence; `user_addressing_agent` permits speech. |
| VAD `speech_threshold` | 0.5 | spec silent on numeric | `turn_detector_vad.py:26, 372` | EXTRA_TO_CODE | Spec Part 3 names Silero VAD but doesn't pin the threshold. Implementation-config also silent (`turn_detector_vad.py:24-25`). |
| VAD `silence_onset_ms` | 300 | spec silent | `turn_detector_vad.py:27, 373` | EXTRA_TO_CODE | Tuning constant. |
| VAD `frame_duration_ms` | 32 | spec silent | `turn_detector_vad.py:374` | EXTRA_TO_CODE | Per-frame cadence; informs latency budgets. |
| Backchannel `emit_threshold` | 0.3 | spec silent | `backchannel_classifier.py:89` | EXTRA_TO_CODE | Introduced by PR #134 to fix Finding 3 (drain saturation during silence). Distinct from the policy-layer `_BACKCHANNEL_THRESHOLD=0.7`. |
| SmartTurn `silence_onset_ms` | 300 | spec silent | `turn_detector_smart.py:46, 87` | EXTRA_TO_CODE | Mirrors VAD onset for consistency. |
| SmartTurn `silence_rms_threshold` | 100 | spec silent | `turn_detector_smart.py:47, 88` | EXTRA_TO_CODE | int16-scale energy cutoff. |
| `proposal_batch_window_ms` | 80 default; 200 in live | spec silent | `realtime_orchestrator.py:175, 199`; `live_pipeline.py:312` | EXTRA_TO_CODE | Grace window for first proposal arrival in T4 before `synthesis_skipped_no_proposal` fires. |
| `hard_cancel_after_ms` | 120 | spec line 913 (200ms p95 gate ≥ this) | `realtime_orchestrator.py:176, 200, 737` | CORRECT | Bounded fall-back hard-cancel after graceful stop attempt. Tighter than the 200ms p95 gate so the gate has headroom. |

### §2.5 — Spec Part 8 latency budgets

| Parameter | Spec gate | Code site | Status | Notes |
|---|---|---|---|---|
| `direct_question_latency` p50 | <800ms | `live_loop_metrics.py:176, 250, 269` | CORRECT (gate wired) | Gate threshold encoded; measurement requires real session data. |
| `direct_question_latency` p95 | <1500ms | `live_loop_metrics.py:177, 251, 270` | CORRECT (gate wired) | Same. |
| `vad_detected_user_speech_to_stop_ms` p95 | <200ms | `live_loop_metrics.py:337, 354, 583` | CORRECT (gate wired) | Spec line 913. |
| `physical_user_speech_onset_to_stop_ms` p95 | <350ms v0.1a → <250ms v0.1b | `live_loop_metrics.py:368` | PARTIAL | Threshold 350 encoded; tightening to 250 by v0.1b not yet reflected in code. |
| `policy_replay_match_rate` | 100% | spec line 907 | n/a in this audit | Gated by `test_policy_replay_exact`. |
| `orphan_action_count` | 0 | spec line 908; `causal_graph.py:44-74` | CORRECT | Gated by `test_causal_graph_completeness`. |
| `false_interruption_count_per_10_min` | <1 | spec line 916 | n/a in this audit | Gated by `test_false_interruption_rate`. |

### §2.6 — TTS / audio output

| Parameter | Value | Spec ref | Code site | Status | Notes |
|---|---|---|---|---|---|
| Kokoro `sample_rate` | 24000 Hz | spec silent on TTS sample rate | `tts_kokoro.py:77, 142-145`; `server.py:69` | CORRECT | Documented; AudioOutBroker envelope carries the value (`server.py:197`). |
| `sample_format` | `pcm_s16le` | spec silent | `server.py:70, 198`; `tts_kokoro.py:139-140` | CORRECT | Documented in browser via `"sample_format"` field; browser only accepts this format (`index.html:236-239`). |
| `allowed_prosody_tags` honoring | n/a (ignored) | spec line 245, Part 9 `ProsodyController` | `tts_kokoro.py:124-126` | MISUSE | Tags are accepted but ignored. Spec's `ProsodyController` adapter slot is filled by `TtsAdapter` per `tts_adapter.py` docstring; semantic honoring deferred to CosyVoice2 / MiniCPM-o native. |
| `assistant_audio_stop_completed` timing | <200ms after `vad_user_speech_onset` | spec line 913 | `audio_output_controller.py:141-147`; `realtime_orchestrator.py:715-754` | CORRECT (gate wired) | Two-stage stop (graceful `request_stop` then hard cancel) ensures bounded latency. |
| `audio_chunk` envelope | as defined | spec silent | `server.py:192-199` | EXTRA_TO_CODE | Wire format introduced by PR #132; spec doesn't pin a wire format for audio fan-out. |

### §2.7 — Event types introduced for the audio path

| Event | Spec ref | Code site | Status | Notes |
|---|---|---|---|---|
| `raw_audio_chunk` | spec line 100 (payload_kind) | `input_ingest.py:142-161` | CORRECT | |
| `vad_frame` | spec silent on event_type names | `turn_detector_vad.py:83, 117` | EXTRA_TO_CODE | Per-frame; spec assumes detectors emit signals but doesn't enumerate event types. |
| `vad_turn_signal` | spec silent | `turn_detector_vad.py:106` | EXTRA_TO_CODE | |
| `smart_turn_invocation` | spec silent | `turn_detector_smart.py:135` | EXTRA_TO_CODE | |
| `smart_turn_signal` | spec silent | `turn_detector_smart.py:145` | EXTRA_TO_CODE | |
| `backchannel_classification` | spec silent | `backchannel_classifier.py:109` | EXTRA_TO_CODE | |
| `vad_user_speech_onset` | spec silent | `realtime_orchestrator.py:357` | EXTRA_TO_CODE | Task 6 barge-in trigger. |
| `barge_in_trigger_no_op` | spec silent | `realtime_orchestrator.py:726` | EXTRA_TO_CODE | |
| `turn_signal_coalesced` | spec silent | `realtime_orchestrator.py:384` | EXTRA_TO_CODE | Coalescing guard (edge case i). |
| `policy_decision` | implied by `SpeakDecision` schema (spec line 235) | `realtime_orchestrator.py:489` | CORRECT | |
| `decision_trace_emitted` | implied by `DecisionTrace` schema (spec line 113) | `realtime_orchestrator.py:521` | CORRECT | |
| `policy_decision_error` | spec silent | `realtime_orchestrator.py:470` | EXTRA_TO_CODE | Defensive fall-through on `decide()` exception. |
| `assistant_generation_start` | spec line 300 | `audio_output_controller.py:84` | CORRECT | |
| `assistant_generation_cancel_requested` | spec line 301 | `audio_output_controller.py:125` | CORRECT | |
| `assistant_audio_buffer_queued` | spec line 302 | `audio_output_controller.py:91` | CORRECT | |
| `assistant_audio_buffer_flushed` | spec line 303 | `audio_output_controller.py:96` | CORRECT | |
| `assistant_audio_stop_requested` | spec line 304 | `audio_output_controller.py:105` | CORRECT | |
| `assistant_audio_stop_completed` | spec line 305 | `audio_output_controller.py:141-145` | CORRECT | |
| `log_drop_or_degrade` | spec line 306 | `event_logger.py:88-107`; `realtime_orchestrator.py:121-139` | CORRECT | Backpressure sentinel. |
| `synthesis_skipped_no_proposal` | spec silent | `realtime_orchestrator.py:652` | EXTRA_TO_CODE | T4 grace-window timeout path. |
| `tts_adapter_error` | spec silent | `realtime_orchestrator.py:677-683` | EXTRA_TO_CODE | |
| `asr_transcript_emitted` | spec silent | **NOT EMITTED** | MISSING_FROM_CODE | Whisper transcript is consumed by `_detect_explicit_remember` (`realtime_orchestrator.py:535`) but is never emitted as an Event. Invariant #1 says "every signal that drives a decision is recorded with provenance"; transcript drives explicit-remember and is the implicit driver of any future addressing refinement. See §3 below. |

---

## §3 — Identified gaps and proposed corrections

For every non-CORRECT row in §2, what the spec says, what the code does, the proposed correction, and the scope.

### §3.1 — `user_addressed_agent` (PARTIAL)

- **Spec.** Field defined at `architecture-v0.1.md:225` as `user_addressed_agent: bool`. Spec is silent on how it is derived independently of `social_mode`. `social_mode = "user_addressing_agent"` is enumerated as the first-class addressing signal (spec line 799). `_BLOCKING_SOCIAL_MODES` defends the three non-addressing-agent values (`speak_policy.py:22-26`).
- **Code.** `manual_test_console/live_pipeline.py:244` derives `user_addressed_agent = (social_mode == "user_addressing_agent")`. Post-PR-#142; replaces PR #140's hardcoded `True`.
- **Correction.** Per-utterance refiner once a detector exists. Issue #139 names three candidates:
  1. ASR-keyword heuristic (cheap; recall-limited).
  2. Foreground-proposal-derived flag (MiniCPM-o emits an "is the user talking to me" classification; couples to Thinker adapter).
  3. Trained addressing classifier (whisper transcript + lightweight classifier).
- **Scope.** v0.1g+ candidate per issue #139. Pre-implementation requirement: spec amendment recording the chosen mechanism. Until then, mechanical derivation is the most defensible interpretation.

### §3.2 — `social_mode` (PARTIAL)

- **Spec.** Spec line 799 enumerates 4 values: `user_addressing_agent | user_addressing_other | group_conversation | background_presence`. Part 7 line 794 names `social_mode` as "first-class state that mutate[s] policy thresholds and memory write permissions." Spec is silent on detector mechanism.
- **Code.** `live_pipeline.py:236` hardcodes `"user_addressing_agent"`. `_BLOCKING_SOCIAL_MODES` (`speak_policy.py:22-26`) is the live-checked gate.
- **Correction.** Real multi-party detector. Plausible mechanisms (no project-lead decision yet):
  1. Speaker diarization on the ASR audio buffer.
  2. Visual face count + gaze tracking on VisionSidecar frames.
  3. Conversational-pattern classifier (turn lengths, overlap).
- **Scope.** Spec amendment to name the detector mechanism + v0.1g+ implementation.

### §3.3 — `retrieved_items` / memory wiring (PARTIAL)

- **Spec.** Spec lines 514-516 describe Stage 4 retrieval; `MemoryItem` schema at spec line 259. Spec line 162 of the dataclass adds `retrieved_items: list[MemoryItem]` to `PolicyInputs`.
- **Code.** `realtime_orchestrator.py:412-446` runs retrieval against `episodic_store` and `semantic_store`. Live-pipeline factory does not pass these stores → both are `None` → `retrieved_items = []` always. Memory write candidates emit on explicit-remember (`realtime_orchestrator.py:535-563`) but are not committed.
- **Correction.** Wire the four-store memory architecture per `docs/plan-memory-wiring-followup.md` (converged 2026-05-15). Per-session stores under `<blob_dir>/<session_id>/memory/{session,core,episodic,semantic}/`. Pass into the orchestrator. Extend `DuplexModel.process_stream` to accept per-call `context_items`.
- **Scope.** v0.1f task; plan ready, not yet implemented.

### §3.4 — Visual signals: `scene_change_score`, `deictic_reference`, `audio_visual_conflict_score`, `grounding_confidence`, `deictic_ambiguous` (all MISSING_FROM_CODE)

- **Spec.** Spec line 156 introduces `audio_visual_conflict_score`. Lines 444-454 define `test_audio_visual_conflict`, `test_current_frame_grounding`, `test_ambiguous_deictic_refusal`. Part 3 names `VisionSidecar` and `DeicticDetector` adapters.
- **Code.** All five fields hardcoded to "no signal" defaults in `_live_policy_inputs_builder` (`live_pipeline.py:242-256`). VisionSidecar wired (PR #141) but its `SceneScorer` and `GroundingModel` are `_NullSceneScorer` / `_NullGroundingModel` (`server.py:115-126`); they always return `0.0` / `("", 0.0)`. Anchor 5 of `docs/plan-vision-sidecar-wiring.md` explicitly defers real models.
- **Correction.** Replace `_NullSceneScorer` with CLIP-cosine or an Implementation-config-named scene-change model (Part 9 line 968). Replace `_NullGroundingModel` with `MiniCPMStreamingModel.streaming_prefill(image=...)` (Anchor 5a of the vision plan) or XLLM lightweight deictic detector (Part 9 line 970). Populate `scene_change_score` from VisionSidecar `ingest_frame()` return; populate `deictic_reference` from a DeicticDetector pass on the transcript or proposal.
- **Scope.** v0.1g+ candidate; significant model wiring work.

### §3.5 — `urgency_score` (MISSING_FROM_CODE)

- **Spec.** `urgency_score` (spec line 226) feeds the alert-threshold gate (`speak_policy.py:64-66`). Spec line 478 maps `cooking → low`, `crisis_emergency → low`. `test_cooking_alert` (spec line 497) exercises the alert path.
- **Code.** Hardcoded `0.0` in `live_pipeline.py:245`. Alert branch never fires.
- **Correction.** Spec is silent on derivation. Plausible mechanisms: visual safety classifier (smoke / boiling-over), audio-keyword detector on transcript, sensor-fusion. Project-lead decision required.
- **Scope.** Spec amendment + implementation. Out of v0.1f scope.

### §3.6 — `attachment_risk_level` (MISSING_FROM_CODE)

- **Spec.** Spec Part 7 lines 838-857 define the AttachmentRiskMonitor. Six tracked signals (lines 843-849). PolicyInputs field at line 233.
- **Code.** Hardcoded `0.0` in `live_pipeline.py:252`. `AttachmentRiskSignal` dataclass exists (`schemas.py:201-214`) but no monitor or emitter is wired.
- **Correction.** v0.1g Anchor 2 (`docs/roadmap-v0.1g-draft.md`) names the schema and `attachment_risk_signal` event type. Task 7 of that roadmap implements the AttachmentRiskMonitor.
- **Scope.** v0.1g (Stage 6).

### §3.7 — `proactivity_budget_remaining`, `cooldown_state` (MISSING_FROM_CODE)

- **Spec.** Budget enforcement appears in spec lines 481-488 (aesthetic_reaction_budget per mode). PolicyInputs at lines 227, 232.
- **Code.** Both hardcoded `{}` in live builder. `speak_policy.py:133-145` reads `proactivity_budget_remaining.get("short_reaction", 0)`. `speak_policy.py:169` reads `cooldown_state.get("aesthetic_reaction", 0)`. Both reads return `0` against the empty dict → silence fallthrough.
- **Correction.** Per-session budget/cooldown state tracker on the orchestrator. Decrement on emission; increment by elapsed time. v0.1f Anchor (roadmap-v0.1f) locked the `proactivity_budget_remaining` alphabet as empty-set at v0.1f, deferring real population to v0.1g.
- **Scope.** v0.1g (Stage 6 texture).

### §3.8 — `allowed_prosody_tags` (PARTIAL)

- **Spec.** Spec line 245 defines the field on `SpeakDecision`. Part 9 (line 974) names `CosyVoice2 expressive tag set` for the ProsodyController.
- **Code.** Always `[]` from `decide()` (no producer). `KokoroTtsAdapter` accepts and ignores tags (`tts_kokoro.py:124-126`).
- **Correction.** Either swap to a TTS backend that honors tags (CosyVoice2, MiniCPM-o native `as_duplex` audio) or document the v0.1 deferral. The `TtsAdapter` Protocol is unchanged across backends.
- **Scope.** v0.2 candidate per Part 11 line 1057. Out of v0.1 scope.

### §3.9 — ASR transcript logging (MISSING_FROM_CODE, invariant #1 concern)

- **Spec.** Invariant #1: "Every input event, signal, decision, and action is timestamped, source-attributed, and recorded with its causal predecessors." `user_transcript` drives `_detect_explicit_remember` and is the implicit driver of any future addressing refinement.
- **Code.** Transcript is computed at `realtime_orchestrator.py:403-407` and consumed at `realtime_orchestrator.py:535-563` (explicit-remember). It is **never emitted as an Event**. Decision trace does not reference it.
- **Correction.** Emit an `asr_transcript_emitted` event after `transcript = self._asr_model(...)` with the transcript text in a `SensitiveField` (`SensitiveField` schema at `schemas.py:77-85` handles free-text routing per CLAUDE.md "Free-text fields go through `SensitiveField`"), `caused_by=[signal_evt_id]`, `payload_kind="signal"`. Also reference the transcript event from `DecisionTrace` so `/decision_traces/<id>.json` carries the audit context.
- **Scope.** Small, additive fix. Suggested v0.1f follow-up (no new model wiring required).

### §3.10 — `assistant_speaking` (PARTIAL)

- **Spec.** Field at spec line 222.
- **Code.** Hardcoded `False` in `live_pipeline.py:241`. Real signal available at `audio_output.is_playing` (`audio_output_controller.py:152-154`) but never threaded into PolicyInputs.
- **Correction.** Plumb `audio_output.is_playing` into the builder. Small change. May enable future "wait until I finish speaking" behavior.
- **Scope.** v0.1f follow-up.

### §3.11 — `privacy_mode`, `current_task_mode`, `risk_mode` (PARTIAL)

- **Spec.** Spec lines 797-803 enumerate the 7 + 7 + 5 values. Part 7 line 794 names these as "first-class state that mutate policy thresholds and memory write permissions."
- **Code.** All three hardcoded `"normal"` in `_live_policy_inputs_builder`. No runtime channel for the user to set them. Issue #96 tracks adapter-routing validation for `local_only`.
- **Correction.** Surface these as session-level user commands (invariant #7: "user commands are first-class"). Plumb via the ingest WebSocket envelope. Implement at the session-state layer.
- **Scope.** v0.1f / v0.1g — depends on user-command surfacing milestone.

### §3.12 — `aesthetic_novelty_score`, `quiet_mode_active`, `short_response_appropriate` (PARTIAL / MISSING_FROM_CODE)

- **Spec.** Schema defaults at `schemas.py:158-162`. Spec line 159 references `quiet_mode_active` (invariant #7 first-class commands).
- **Code.** Defaults are used (dataclass `field` defaults); never overridden by live builder.
- **Correction.** `aesthetic_novelty_score` requires a ThinkerProposalGen output (Stage 6 / v0.1g). `quiet_mode_active` is a user command — same surfacing path as §3.11. `short_response_appropriate` requires a trigger detector (e.g., visual surprise + recent silence).
- **Scope.** v0.1g (texture).

### §3.13 — `tool_progress_evidence` (MISSING_FROM_CODE)

- **Spec.** v0.1f Anchor 4 (`docs/roadmap-v0.1f-draft.md`). Field on `PolicyInputs` per `schemas.py:163`.
- **Code.** Stage 5 not yet active in the live pipeline; field always `None`.
- **Scope.** v0.1f Tasks 2-15 (pending).

---

## §4 — Spec-amendment candidates

Parameters where the spec is silent on derivation and a real implementation requires a project-lead decision. For each, candidate mechanisms (not picking one).

### §4.1 — Per-utterance `user_addressed_agent` derivation

Spec defines the field (line 225) and the addressing-related `social_mode` value (line 799). Spec is silent on how to refine `user_addressed_agent` per utterance once a stable transcript is available.

Candidate mechanisms:

1. **Wake-word / hot-phrase detector** — e.g., "hey companion" / agent name in `user_transcript`. Requires no new model; classification is a string check or regex.
2. **MiniCPM-derived classifier** — prompt MiniCPM-o to emit an "is the user talking to me" token in its proposal stream, or read a hidden-state feature.
3. **ASR-keyword heuristic** — interrogative structure, second-person pronouns, agent-name mentions.
4. **Speaker diarization** — gate on whether the speaker matches the registered user (vs guest).
5. **Visual gaze tracking** — eye contact toward the camera.

Issue #139 enumerates options 1-3.

### §4.2 — `social_mode` detector

Spec enumerates the four values but is silent on detection. Candidates:

1. **Speaker diarization** on the ASR audio buffer.
2. **Visual face count + gaze tracking** on VisionSidecar frames.
3. **Conversational-pattern classifier** (turn lengths, overlap, alternation).
4. **Default-from-context** — assume `user_addressing_agent` unless multiple distinct speakers are detected.

### §4.3 — `urgency_score` derivation

Spec gates `alert` on this score (spec lines 476-480, code `speak_policy.py:64-66`) but is silent on derivation. Candidates:

1. **Visual safety classifier** — smoke / fire / boiling-over detection.
2. **Audio keyword detector** — alarms / shouting / "help" / "stop".
3. **Multi-modal fusion** — sensor-fusion across audio + video + ambient sensors.

### §4.4 — `current_task_mode` detector

Spec enumerates `cooking`, `crisis_emergency`, `creative_focus`, `normal`, `walking_outdoor`, `sleep_winddown`, `group_unaddressed`. Candidates:

1. **User command surfacing** — explicit mode-set via "I'm cooking" / "writing" / "going to sleep" (invariant #7 alignment).
2. **Visual scene classifier** — kitchen / desk / outdoor / bedroom.
3. **Calendar / time-of-day heuristic** — work hours, late-evening.

### §4.5 — Backchannel `emit_threshold` tuning

Empirical constant introduced by PR #134 (Finding 3 fix). Spec is silent. The trade-off between drain saturation (too-low threshold) and missed backchannels (too-high threshold) is project-empirical; a value of 0.3 was the immediate fix. A spec amendment could pin the trade-off, or leave it as a tuning knob.

### §4.6 — VAD numeric thresholds

`speech_threshold=0.5`, `silence_onset_ms=300`, `frame_duration_ms=32`. Spec names Silero VAD (Part 9 line 962) but pins no numbers. Implementation-config likewise silent. Spec amendment could either pin the values or explicitly leave them as tuning knobs.

### §4.7 — Prosody tag honoring

Spec defines `allowed_prosody_tags` on `SpeakDecision` (line 245) and names `CosyVoice2 expressive tag set` in Part 9 (line 974). Today Kokoro ignores tags entirely. Spec amendment could either accept v0.1 deferral explicitly or require a backend swap before v0.2.

---

## §5 — Path coverage matrix

Mapping each spec contract test to live-pipeline exercise status. Legend:

- ✓ — live-pipeline-exercisable (real audio → real signals → real decision)
- ⚠️ — partial (some inputs reproduce; some signals don't refine)
- ✗ — requires fixtures + signals not wired in the live path

| Test | Status | Notes |
|---|---|---|
| `test_explicit_turn_handoff` | ✓ | EOU + addressing-agent → full_response. Exercisable now (post-#142). |
| `test_thinking_pause` | ⚠️ | SmartTurn v3 wired (`turn_detector_smart.py`). Real mid-thought continuation requires a real second utterance window; exercisable in fixtures. Spec defect issue #10 amendment defers to v0.1b. |
| `test_backchannel_survival` | ✓ | Backchannel classifier wired (`backchannel_classifier.py`, emit_threshold=0.3). |
| `test_not_addressed_to_me` | ⚠️ | `_BLOCKING_SOCIAL_MODES` gate works at the policy layer. But live pipeline hardcodes `social_mode="user_addressing_agent"` → cannot reproduce "two humans talking near device" without a real multi-party detector. |
| `test_barge_in` | ✓ | T2's `_VadOnsetFrame` path → `_fire_barge_in` → `request_stop` → `cancel_generation` after 120ms. Exercisable now. |
| `test_direct_question_latency` | ✓ | Latency gate wired (`live_loop_metrics.py:144-176`). Measurement requires real session. |
| `test_cooking_alert` | ✗ | `urgency_score` hardcoded `0.0`; `current_task_mode` hardcoded `"normal"`. Both required by alert branch. |
| `test_creative_focus_silence` | ✗ | `current_task_mode` hardcoded `"normal"`. Cannot enter creative_focus mode in live pipeline. |
| `test_aesthetic_cooldown` | ✗ | `aesthetic_novelty_score` not populated; `cooldown_state` empty. v0.1g (Stage 6) target. |
| `test_proactivity_budget_respected` | ✗ | `proactivity_budget_remaining` empty. v0.1g target. |
| `test_explicit_remember` | ⚠️ | `_detect_explicit_remember` wired (`realtime_orchestrator.py:81-94`). `memory_write_candidate` emitted (`realtime_orchestrator.py:551-563`). But stores are `None`, so no actual commit. |
| `test_explicit_forget` | ✗ | Forget command not detected in live pipeline. Issue #105 tracks tombstone semantics. |
| `test_correction` | ✗ | Correction command not detected in live pipeline. Memory-store wiring required (§3.3). |
| `test_no_camera_memory` | ⚠️ | `VisionSidecar` honors `privacy_mode == "no_camera_memory"` (`vision_sidecar.py:131-134, 161-162, 200-201`). But `privacy_mode` is hardcoded `"normal"` in live builder. |
| `test_guest_present_memory_gate` | ✗ | Privacy gates exist in MemoryManager; `privacy_mode` not surfaceable in live path. |
| `test_sensitive_conversation_retention` | ✗ | Same; mode unsurfaceable. |
| `test_why_did_you_say_that` | ⚠️ | DecisionTrace persisted (`realtime_orchestrator.py:484`). `retrieval_used` field populated only when stores wired (§3.3). Out-of-band b200 read is the only path today. |
| `test_no_latency_regression` | ✓ | Paired measurement framework wired (`live_loop_metrics.py`). |
| `test_audio_visual_conflict` | ✗ | `audio_visual_conflict_score` hardcoded `0.0`. Requires real scoring (§3.4). |
| `test_current_frame_grounding` | ✗ | Grounding model is `_NullGroundingModel`. Vision sidecar plan Anchor 5 defers real model. |
| `test_deictic_continuity` | ✗ | Same; deictic_reference always False. |
| `test_recent_visual_memory` | ⚠️ | VisionSidecar 60s ring buffer wired (`vision_sidecar.py:127-143, 312-315`). Resolution requires real grounding model. |
| `test_hallucination_resistance` | ⚠️ | `grounding_confidence` gate at 0.5 exists (`speak_policy.py:30, 116-117`). Hardcoded `1.0` upstream defeats it. |
| `test_ambiguous_deictic_refusal` | ✗ | `deictic_ambiguous` hardcoded `False`. Requires multi-candidate grounding. |
| `test_temporal_event_order` | ✗ | Requires multi-frame temporal reasoning + visual memory. Stage 2+. |

Summary: out of 25 spec contract tests, 6 are live-exercisable (✓), 7 are partial (⚠️), 12 are fixture-only (✗) in the current live pipeline. The fixture-only set is dominated by missing visual signals, missing mode surfacing, and not-yet-implemented memory wiring.

---

## §6 — Out of scope

The following are explicitly out of scope for this design doc:

- **Stage 5 tools.** `tool_progress_evidence`, `ToolRouter`, evidence-bound filler. Covered by `docs/roadmap-v0.1f-draft.md`.
- **Stage 6 texture.** Aesthetic-reaction rubric, attachment-risk monitor, `recent_shared_moments`. Covered by `docs/roadmap-v0.1g-draft.md`.
- **Multi-user behavior beyond `_BLOCKING_SOCIAL_MODES`.** Group conversation handling, speaker diarization, third-party retention. Spec Part 7 names the modes; detector mechanism is a spec-amendment question.
- **Spec amendments themselves.** This doc surfaces candidates (§4); the project lead decides.
- **Stage 2 real-vision models.** CLIP scoring, MiniCPM-o vision tower for grounding, XLLM deictic detector. Plan at `docs/plan-vision-sidecar-wiring.md` Anchor 5 defers these.
- **CosyVoice2 / MiniCPM-o native TTS swap.** Platform blockers documented at `tts_kokoro.py:11-37`. Not blocking v0.1f.

---

*Design doc written 2026-05-15 against post-PR-#142 working tree (`784990b`). All file:line citations refer to that commit unless explicitly noted.*
