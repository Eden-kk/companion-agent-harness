# Plan: continuous USER-dialogue ASR sidecar (onto main)

## Goal
In main's native continuous console path (`--minicpm-streaming-raw` → `StreamingRawPipeline`),
transcribe the USER's mic audio in a background sidecar and emit
`asr_transcript_emitted` so the dialogue panel shows USER lines (UI already renders them).

## Success criterion
`pytest -k continuous_asr_sidecar` passes (4 tests: emit-on-silence-boundary,
skip-empty-transcript, seam-off-no-emit, multiple-utterances), `py_compile`
clean, and the existing suite stays green.

## Context (verified on main @ 9fe2622)
- UI **done**: `index.html` renders `asr_transcript_emitted` → `appendDialogueRow("USER", …)` (line 407), in DEFAULT_ON filter (1451).
- Event type **NOT registered**: `asr_transcript_emitted` appears only in a comment, not in `EVENT_TYPE_SCHEMAS`. Must add.
- Console continuous path = `StreamingRawPipeline` (live_pipeline.py:785) wrapping `MiniCPMRawStreamingDriver`; built by `build_streaming_raw_pipeline` (806) at server.py:687. `push_audio(frame_bytes, raw_audio_event_id)` already receives user PCM.
- `StageSixEventSchema` fields: payload_kind, subject_class, sensitivity, retention_policy_id, notes, required_fields — match source entry exactly.

## Changes

### (A) Add verbatim from integration branch
1. `companion_harness/asr_sidecar.py` — `ASRSidecar` class. Self-contained: deps = `schemas.Event`, `EventLogger` (typing only), an `asr_model` callable, optional `config_store.get_seam("asr")`. Copy as-is.
2. `tests/test_continuous_asr_sidecar.py` — 4 CPU tests, construct `ASRSidecar` directly with a fake asr_model + `asyncio.Queue`. No pipeline dependency. Copy as-is.

### (B) Event schema — NO CHANGE (revised per plan-critic)
3. **DROP.** `asr_transcript_emitted` is ALREADY registered in `companion_harness/v0_1f_event_schema.py:214` (`ToolEventSchema`, required_fields=`(transcript_text, signal_event_id, asr_model_label)`). Do **NOT** add it to `v0_1g` — `tests/test_v0_1g_event_schema.py:81` asserts `set(EVENT_TYPE_SCHEMAS.keys()) == EXPECTED_EVENT_TYPES` (exact set, excludes it) → would break.
   - **Payload form:** `required_fields` is NOT enforced at runtime (no `.required_fields` use outside schema files; `EventLogger.log()` does not validate). The sidecar emits `payload_inline={"text_preview": transcript[:200]}` — the SAME sanctioned console-display field the turn-based emitter uses (`realtime_orchestrator.py:736-743`) and the ONLY field the UI reads (`index.html:352,406`). Keep the integration `ASRSidecar._emit` as-is (text_preview preview + payload_hash). Governed full-transcript storage (SensitiveField behind payload_ref) is the turn-based path's concern and is OUT OF SCOPE here; document this divergence in the sidecar wiring comment.

### (C) Wire sidecar into the native path (additive; driver untouched)
4. `manual_test_console/live_pipeline.py`:
   - import `ASRSidecar`.
   - `StreamingRawPipeline`: add optional fields `_asr_sidecar=None`, `_asr_queue=None`, `_asr_task=None`.
   - `start()`: after `driver.start()`, if `_asr_sidecar`: `_asr_task = asyncio.create_task(_asr_sidecar.run())`.
   - `stop()`: if `_asr_queue`: `put_nowait((b"", ""))` sentinel; if `_asr_task`: await it (with cancel fallback) BEFORE `driver.stop()` so trailing utterance flushes.
   - `push_audio()`: **FIX SIGNATURE (Blocker 2)** — server.py:781 calls `pipeline.push_audio(payload_bytes, evt.event_id, ts_mono)` (3 args) but current signature is `(frame_bytes, raw_audio_event_id)` (2) → latent TypeError in streaming-raw on main. Change to `def push_audio(self, frame_bytes, raw_audio_event_id, ts_mono_ms: int = 0)`; call `self.driver.push_audio(frame_bytes, raw_audio_event_id)` (driver takes 2; ts_mono_ms accepted-and-ignored). Then if `_asr_queue`: `put_nowait((frame_bytes, raw_audio_event_id))`; on `asyncio.QueueFull` drop-oldest (`get_nowait()` then `put_nowait()`) — never block (inv #10).
   - `build_streaming_raw_pipeline(...)`: add param `asr_model: Any = None`; if not None, create `asyncio.Queue(maxsize=512)` + `ASRSidecar(session_id=session_id, asr_model=asr_model, logger=shielded_logger, config_store=None, queue=queue)` and pass both into the `StreamingRawPipeline(...)`.

### (D) Server: load + pass ASR in streaming-raw mode
5. `manual_test_console/server.py`:
   - streaming_raw branch (line 687 `build_streaming_raw_pipeline(...)`): add `asr_model=request.app[KEY_ASR_MODEL]`.
   - **Blocker 3 — exact change:** in the `if args.minicpm_streaming_raw:` factory block the line is `asr_factory: Optional[Callable[[], Any]] = None`. Replace with: define `_asr_lang = None if args.language == "auto" else args.language` and set `asr_factory = lambda: _load_asr_model(_asr_lang, getattr(args, "asr_backend", "faster_whisper"))`. This populates `KEY_ASR_MODEL` (and lets `--asr-backend deepinfra` work here). If ASR fails to load, `KEY_ASR_MODEL` stays None → `build_streaming_raw_pipeline(asr_model=None)` → no sidecar (graceful, no behavior change).
   - Update the streaming-raw startup WARNING (~server.py:2296) that says it bypasses ASR: clarify it now runs a **read-only** USER-transcript sidecar (does not gate speech; SpeakPolicy/addressing still bypassed).

### (E) No change
6. `index.html` — already renders USER rows.

## Invariants / risks
- **inv #10 (non-blocking realtime path):** push_audio uses `put_nowait` + drop-oldest; ASR runs in the sidecar's `ThreadPoolExecutor`. The driver path is unchanged.
- **inv #1/#3 (DAG closes):** `caused_by=[raw_audio_event_id]` (last frame's id); that id is the ingest raw-audio event the driver already uses — valid predecessor.
- **StreamingRawPipeline "demo/ASR-free" purpose:** that label is about bypassing SpeakPolicy/addressing on the *speak* path. The sidecar is **read-only** (emits an audit event for the panel), does not gate or alter speech — consistent. Comment this at the wiring site.
- **Risk:** if `KEY_ASR_MODEL` is None in streaming-raw (ASR failed to load / stubs), `build_streaming_raw_pipeline` gets `asr_model=None` → no sidecar, no behavior change (graceful).

## Out of scope
- The `--continuous`/`ContinuousLivePipeline` architecture (integration branch) is NOT ported.
- No diarization, no addressing, no policy changes.
