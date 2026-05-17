# API Tests — Operator Reference

Live-console endpoint reference and per-plan validation suites for autonomous
post-deploy verification.  Tests are manual (operator-triggered).  Run via:

```
python tests/manual/api_test_runner.py --plan 1 --test 1.2
```

Server default: `http://localhost:8800`.  Use `--base-url` to override.

---

## Endpoints

### GET /healthz

Returns JSON status of all adapters and counters.

```json
{
  "status": "ok",
  "mode": "live | stubs | capture_only | minicpm_streaming_raw",
  "minicpm_loaded": true,
  "live_pipeline_enabled": true,
  "active_sessions": 0,
  "use_stubs": false,
  "vad_model": "<label>",
  "smart_turn_model": "<label>",
  "backchannel_model": "<label>",
  "asr_model": "<label>",
  "tts_model": "<label>",
  "vad_ready": true,
  "smart_turn_ready": true,
  "backchannel_ready": true,
  "asr_ready": true,
  "tts_ready": true,
  "foreground_model_ready": true,
  "vision_enabled": false,
  "vision_ready": false,
  "frames_buffered": 0,
  "last_frame_event_id": null,
  "sessions_opened": 0,
  "chunks_ingested": 0,
  "frames_ingested": 0,
  "audio_out_enabled": true,
  "audio_out_chunks_sent": 0,
  "logger_drain_running": true,
  "scene_scorer": "stub:_NullSceneScorer",
  "grounding_model": "stub:_NullGroundingModel",
  "av_conflict_scorer": "stub:_NullAudioVisualConflictScorer",
  "deictic_model": "stub:_NullDeicticModel",
  "urgency_scorer": "stub:_NullUrgencyScorer",
  "embedder": "stub:_NullEmbeddingAdapter",
  "gpu_memory_allocated_mb": null,
  "gpu_memory_reserved_mb": null,
  "gpu_memory_total_mb": null,
  "gpu_device_name": null,
  "events_per_second_last_60s": 0,
  "events_total_since_start": 0,
  "blob_rotation_alive": false,
  "blob_rotation_last_tick_wall": null
}
```

`vad_ready`, `asr_ready`, etc. are `true` iff the label does **not** start with
`stub:`.  `minicpm_loaded` is `true` iff the foreground model object is non-null.

---

### GET /metrics

Prometheus text format (Content-Type: `text/plain; version=0.0.4`).

Metrics exposed: `harness_uptime_seconds`, `harness_events_per_second_last_60s`,
`harness_adapter_ready{adapter="vad|smart_turn|backchannel|asr|tts|vision"}`,
`harness_gpu_memory_allocated_mb`.

---

### GET /config

Returns current Tier-B runtime values plus schema metadata.

```json
{
  "values": { "<key>": <value>, ... },
  "schema": {
    "<key>": {
      "default": <value>,
      "min": <number>,
      "max": <number>,
      "step": <number>,
      "value_type": "float|int",
      "description": "<string>",
      "code_location": "<string>"
    }
  },
  "seams": { "<seam_name>": true|false, ... }
}
```

---

### POST /config/patch

Apply one Tier-B override.

Request body: `{"key": "<key>", "value": <number>}`

Response (200):
```json
{
  "key": "<key>",
  "previous_value": <number>,
  "new_value": <number>,
  "operator_action_event_id": "<event_id>",
  "config_change_event_id": "<event_id>"
}
```

Errors: 400 (type/range), 403 (Tier-A key or unknown key).

---

### POST /config/reset

Reset one key, one section, or all Tier-B keys to defaults.

Request body: `{"key": "<key>"}` or `{"section": "<section>"}` or `{}` (reset all).

Response (200):
```json
{
  "changes": [{"key": ..., "previous_value": ..., "new_value": ...}, ...],
  "operator_action_event_id": "<event_id>",
  "config_change_event_ids": ["<event_id>", ...]
}
```

---

### GET /config/seams

Returns enabled/disabled state of all hot seams.

```json
{
  "seams": [
    {"seam": "<seam_name>", "enabled": true|false},
    ...
  ]
}
```

---

### POST /config/model-swap

Toggle a hot-seam enabled/disabled state at runtime.

Request body: `{"seam": "<seam_name>", "enabled": true|false}`

Response (200):
```json
{
  "accepted": true,
  "model_swap_event_id": "<event_id>",
  "restart_required": false,
  "requested_at_ms": <int>,
  "applied_at_ms": <int>,
  "latency_ms": <int>
}
```

Errors: 400 (invalid `enabled` value), 403 (unknown seam).

Emits three events on `/ws/display`: `operator_action` → `model_swap_requested`
→ `model_swap_completed` (or `model_swap_rejected` on error).

---

## WebSocket: /ws/display

Read-only fan-out stream.  Server pushes every harness `Event` as a JSON object.

**Connection**: `GET ws://localhost:8800/ws/display`  
**Direction**: server → client only (client sends nothing)  
**Heartbeat**: 30 s ping/pong  
**Max message size**: 8 MiB

Each message is a JSON object with this envelope:

```json
{
  "event_id": "<uuid-hex>",
  "event_type": "<string>",
  "timestamp_wall": "<ISO-8601>",
  "timestamp_mono_ms": <int>,
  "caused_by": ["<event_id>", ...],
  "payload_kind": "signal|transcript|raw_audio|raw_video|model_output|memory_op|tool_event",
  "subject_class": "self|third_party|mixed|unknown",
  "sensitivity": "safe|sensitive|highly_sensitive",
  "retention_policy_id": "<string>",
  "payload_inline": { ... }
}
```

### Core event types

| event_type | Trigger | Key payload fields |
|---|---|---|
| `raw_audio_chunk` | Every 30ms audio frame ingested | `sample_rate`, `num_samples` |
| `vad_frame` | Per audio frame from VAD | `p_speech` (float) |
| `vad_user_speech_onset` | VAD onset detected | — |
| `vad_turn_signal` | VAD end-of-utterance | `p_done` (float) |
| `vad_signal_suppressed_by_smart_turn` | SmartTurn vetoed VAD EOU | — |
| `backchannel_classification` | Per frame from backchannel model | `p_backchannel` (float) |
| `asr_transcript_emitted` | ASR decodes utterance | `transcript` (SensitiveField), `lang` |
| `addressing_classified` | Addressing classifier result | `addressed` (bool), `confidence` (float), `classifier_name` |
| `addressing_classifier_low_confidence` | Classifier ambivalent | `confidence` (float), `transcript_preview` |
| `foreground_proposal` | Thinker emits candidate | text content (SensitiveField) |
| `policy_decision` | SpeakPolicy decides action | `action_type` (silence/backchannel/short_reaction/full_response), `primary_reason_code` |
| `policy_decision_error` | Policy evaluation error | `error` |
| `tts_synthesis_started` | TTS begins synthesizing | — |
| `tts_synthesis_completed` | TTS synthesis done | — |
| `tts_synthesis_cancelled` | TTS interrupted mid-stream | — |
| `assistant_audio_buffer_queued` | TTS chunk queued to audio output | `num_samples`, `sample_rate` |
| `assistant_audio_buffer_flushed` | Audio output drain complete | — |
| `memory_retrieval_event` | Memory query executed | — |
| `memory_write_candidate` | Candidate memory item | — |
| `explicit_forget` | User issued forget command | — |
| `operator_action` | HTTP operator request received | `endpoint` |
| `config_change` | Tier-B value changed | `key`, `previous_value`, `new_value` |
| `model_swap_requested` | Seam toggle requested | `seam`, `from_enabled`, `to_enabled` |
| `model_swap_completed` | Seam toggle applied | `seam`, `latency_ms` |
| `model_swap_rejected` | Seam toggle rejected | `seam`, `reason` |
| `log_drop_or_degrade` | EventLogger backpressure | — |
| `audio_tee_drop_summary` | Audio tee overflow | — |
| `synthesis_skipped_no_proposal` | Proposer timed out | `batch_window_ms`, `signal_evt_id` |
| `tts_adapter_error` | TTS adapter threw | `error` |
| `aesthetic_proposal_generated` | Aesthetic-rubric proposal | `rubric_violations` |
| `attachment_risk_signal` | Attachment-risk signal detected | `signal_class` |
| `addressing_classified` | Addressing classifier result | `addressed`, `confidence` |
| `native_duplex_invocation` | MiniCPM streaming call | `is_listen` |
| `minicpm_session_reset` | MiniCPM inter-turn reset | `reset_at_ms`, `trigger` |
| `proposer_token_buffered` | Path-B ring append | `ring_seq`, `is_listen`, `text_preview` |
| `commit_or_discard` | Path-B policy decision | `committed`, `signal_evt_id` |

High-rate types (`raw_audio_chunk`, `vad_frame`) are sampled before display
fanout; the EventLogger ring still records every event.

---

## WebSocket: /ws/ingest

Audio and video input.  One connection = one live-pipeline session.

**Connection**: `GET ws://localhost:8800/ws/ingest`  
**Direction**: client → server  
**Heartbeat**: 30 s ping/pong  
**Max message size**: 4 MiB

Each message is a JSON envelope:

```json
{
  "event_type": "raw_audio",
  "payload_inline_or_ref": "<base64-encoded PCM16 LE bytes>",
  "timestamp_mono_ms": <int>,
  "client_id": "<stable string>",
  "device_label": "<mic label>",
  "timestamp_wall": "<ISO-8601, optional>"
}
```

For video frames use `"event_type": "raw_video"` with base64-encoded JPEG bytes.

Audio format: **PCM16 LE, mono, 16 kHz**.  Recommended chunk size: 30ms (480
samples = 960 bytes).  Trailing silence after speech is required for VAD/EOU
to fire; 800ms is sufficient in practice.

---

## WebSocket: /ws/audio_out

Synthesized audio output.  Read-only fan-out.

**Connection**: `GET ws://localhost:8800/ws/audio_out`  
**Direction**: server → client only  
**Heartbeat**: 30 s ping/pong

Each message:

```json
{
  "type": "audio_chunk",
  "session_id": "<live-pipeline session id>",
  "seq": <int, per-session monotonic>,
  "pcm_bytes_b64": "<base64 raw PCM bytes>",
  "sample_rate": 24000,
  "sample_format": "pcm_s16le"
}
```

---

## Test Fixtures

`api_test_runner.py` generates fixtures on the fly — no pre-recorded audio
required for basic tests.

| Fixture | Generation | Use |
|---|---|---|
| Silence (1 s) | `np.zeros(16000, dtype=np.int16)` | Baseline / keep-alive |
| Speech-like (3 s) | Multi-frequency sine sum (200+400+600+800 Hz) at 0.3 amplitude | Exercises VAD/EOU path |
| Spoken English | TODO — record or use espeak-ng: `espeak-ng -w /tmp/en.wav "Hello"` then resample to 16kHz PCM16 | Tests 1.2, 3.3 |
| Spoken Chinese | TODO — `espeak-ng -v zh -w /tmp/zh.wav "你好"` then resample | Tests 1.3, 1.4 |

The speech-like fixture reliably triggers VAD above the speech-onset threshold.
See `tests/test_vad_silero.py:test_silero_produces_speech_probability_above_zero_on_speech_like_audio`
for the exact recipe (multi-frequency sum).

---

## Plan 1 — Chinese-Ready Stack

**Prerequisite**: PR1 (WhisperX ASR adapter + `--language` flag) has landed.

### Test 1.1 — Console boots with `--language=en`, ASR loaded

```
python tests/manual/api_test_runner.py --plan 1 --test 1.1 --base-url http://localhost:8800
```

**Asserts**:
- `/healthz` returns HTTP 200
- `asr_ready == true`
- `asr_model` does not start with `stub:`

---

### Test 1.2 — English audio → `asr_transcript_emitted`

**Asserts**:
- `asr_transcript_emitted` event fires within 10 s of audio stream end
- `payload_inline.transcript` byte length > 0

---

### Test 1.3 — Chinese audio → ASR does not crash

**Asserts**:
- No `tts_adapter_error` or `policy_decision_error` events within 15 s
- If `asr_transcript_emitted` fires, transcript byte length > 0
- Server remains responsive (`/healthz` still returns 200) after ingestion

---

### Test 1.4 — Chinese backchannel score

**TODO** — requires a backchannel model wired for Chinese.  When implemented:

- Send a short Chinese phrase audio fixture
- Assert `backchannel_classification` event fires with `p_backchannel >= 0.5`

Not testable in this session.

---

## Plan 3 — Tier-2 Latency Wins

**Prerequisite**: PR1 (`torch.compile` warmup path) has landed.

### Test 3.1 — Console boots with `--torch-compile`, MiniCPM loaded within 60 s

```
python tests/manual/api_test_runner.py --plan 3 --test 3.1
```

**Asserts**:
- `/healthz` returns `minicpm_loaded == true` within 60 s of first poll
- `foreground_model_ready == true`

---

### Test 3.2 — Boot log contains compile warmup line

**Asserts**:
- Console stdout/stderr (captured by operator separately) contains the string
  `warmup: minicpm=`

Not assertable via HTTP — operator must check process log manually or pipe
through `tee`.

---

### Test 3.3 — EOU → `tts_synthesis_started` latency < 5000 ms

```
python tests/manual/api_test_runner.py --plan 3 --test 3.3
```

**Method**: send 3 s speech-like audio fixture, record `vad_turn_signal`
timestamp and `tts_synthesis_started` timestamp from `/ws/display`, compute
delta.

**Asserts**:
- `tts_synthesis_started` fires within 5000 ms of `vad_turn_signal`

This is a smoke check only; the 1500 ms target is for after G+I.

---

### Test 3.4 — `scripts/probe_torch_compile_warmup.py` verdict is SHIP

Run separately:

```
python scripts/probe_torch_compile_warmup.py 2>&1 | tee /tmp/probe-out.txt
grep "SHIP\|NO-SHIP" /tmp/probe-out.txt
```

**Asserts**: stdout contains `SHIP` and does not contain `NO-SHIP`.

Not automated in `api_test_runner.py`; operator runs probe independently.

---

## Plan 2 — Native MiniCPM TTS

**Prerequisite**: Native MiniCPM TTS PR has landed.

### Test 2.1 — Console boots with `--tts-adapter native_minicpm`

```
python tests/manual/api_test_runner.py --plan 2 --test 2.1
```

**Asserts**:
- `/healthz` returns HTTP 200
- `tts_model` field equals `MiniCPM-o native TTS`
- `tts_ready == true`

---

### Test 2.2 — Utterance produces full TTS event sequence

```
python tests/manual/api_test_runner.py --plan 2 --test 2.2
```

**Asserts** (within 20 s of stream end):
- `tts_synthesis_started` fires at least once
- `tts_synthesis_completed` fires at least once
- `assistant_audio_buffer_queued` fires at least once

---

### Test 2.3 — First audio chunk has no leading silence (CN7-FIX)

```
python tests/manual/api_test_runner.py --plan 2 --test 2.3
```

**Method**: capture first `audio_chunk` on `/ws/audio_out`, decode
`pcm_bytes_b64`, take first 200 ms (4800 samples at 24 kHz), compute RMS.

**Asserts**: RMS of first 200 ms > 0.01

---

### Test 2.4 — Foreground inference still works in same session (B1-FIX)

```
python tests/manual/api_test_runner.py --plan 2 --test 2.4
```

**Method**: after test 2.2 completes, send a second utterance in the same WS
session.

**Asserts**: `policy_decision` fires within 15 s of second stream end.

---

## Running All Tests for a Plan

```
python tests/manual/api_test_runner.py --plan 1
python tests/manual/api_test_runner.py --plan 2
python tests/manual/api_test_runner.py --plan 3
```

Skippable tests (marked TODO above) are printed as SKIP, not FAIL.  Exit code 0
means all non-skipped assertions passed.
