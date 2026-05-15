# Plan — VisionSidecar wiring (manual-test live pipeline)

## Status: **DRAFT** — awaiting plan-critic round 1 (recovered from shared-worktree race).

> Wire `VisionSidecar` into the manual-test live pipeline so video
> frames reach the foreground model. Real e2e scenarios I–L (deictic,
> audio-visual conflict, `no_camera_memory`) become exercisable. Real
> models for scoring / grounding are intentionally deferred.

## Goal

Wire `VisionSidecar` into the manual-test live pipeline so per-session
video frames flow to the foreground model alongside audio, exercising
the `(audio_bytes, video_bytes | None)` tuple surface that
`DuplexModel.process_stream` already accepts.

## Anchors

### Anchor 1 — `VisionSidecar`-buffered, accessor read by `_bounded_frame_gen`

Preserves the existing `StreamingRealtimeOrchestrator` API (no
widening of the `audio_in` queue). Cites
`realtime_orchestrator.py:555-582` (`_bounded_frame_gen` already
yields `(frame_bytes, None)` tuples) and
`foreground_model.py:144-162` (`DuplexModel.process_stream` already
accepts `tuple[bytes, bytes | None]`). The Protocol surface chosen
in PR #40 anticipated this wiring exactly. Per-session `VisionSidecar`
owns a single most-recent-frame buffer.

### Anchor 2 — Audio-without-video / consume-once semantics

Most audio chunks receive `video_bytes=None`. A video frame is
"fired" once paired with the next audio chunk after arrival, then
cleared from the buffer.

**Determinism:** given recorded inputs (audio chunks + video frames
with timestamps), the pairing is fully deterministic.

**Avoids:** MiniCPM-o re-running its vision tower on the same frame
for 10 consecutive audio chunks.

### Anchor 3 — Flag-controlled `init_vision=True`

New `--enable-vision` CLI flag, default OFF. When ON, the server
constructs a separate `MiniCPMStreamingModel(init_vision=True)`
instance. Backward compat preserved (default-off path matches
current behavior bit-for-bit).

### Anchor 4 — `VisionSidecar` API surface

Two new methods:

- `ingest_frame_bytes(frame_bytes, event_id, timestamp_mono_ms)` —
  buffers frame + logs `vision_frame` event with closed `caused_by[]`.
- `consume_pending_frame() -> tuple[bytes, str] | None` — returns
  most-recent buffered frame + source `event_id`, clears buffer; OR
  `None` if no frame.

Status accessors:

- `frames_buffered() -> int`
- `last_frame_seq() -> int | None`

Inherits existing `no_camera_memory` privacy gate at
`vision_sidecar.py:127-128`.

### Anchor 5 — Scoring/grounding stubs only

- `scene_change_score()` returns `0.0`.
- `deictic_reference` returns `False`.

Real models for AV-conflict scoring and deictic grounding are
deferred to a follow-up issue. Scenarios K and J are cosmetically
broken until that follow-up lands.

## OQs (all leans)

- **OQ-1.** Buffer size — single most-recent frame (lean) vs ring
  buffer (lean: ring buffer is a v0.1c "recent_visual_memory"
  concern, defer).
- **OQ-2.** `DeicticDetector` wiring — lean NO; defer to a separate PR.
- **OQ-3.** Camera-revocation mid-session — lean:
  `consume_pending_frame()` returns `None`; foreground gets
  `video=None`.
- **OQ-4.** Max frame age — lean NO max-age check at v0.1f.
- **OQ-5.** v0.1c contract test coupling — lean: existing deictic
  fixtures use scripted frames, not live capture; should not break.

## Implementation sketch (11 bullets)

1. Extend `VisionSidecar` from its current stub.
2. Wire `raw_video_frame` ingest → `VisionSidecar.ingest_frame_bytes()`.
3. Update `live_pipeline.py` factory to construct one `VisionSidecar`
   per session.
4. Possibly extend `StreamingRealtimeOrchestrator.__init__` to accept
   a `vision_sidecar` parameter (default `None`).
5. Update `MiniCPMStreamingModel.process_stream` to consume
   `(audio_bytes, video_bytes | None)` per Anchor 2.
6. Add `--enable-vision` CLI flag (default OFF) to server entrypoint.
7. Extend the startup banner to report `vision_enabled` state.
8. Extend `/healthz` to report `vision_enabled`, `last_frame_seq`,
   `frames_buffered`.
9. Wire the privacy-gate path (`no_camera_memory`) through
   `VisionSidecar.ingest_frame_bytes()`.
10. Add `vision_frame` event to the schema-known-event-types list
    (already present from v0.1c stub; verify no-op).
11. Document the operator flag in `docs/manual-test-handbook.md`.

## Test plan (4 contract tests)

- `test_vision_sidecar_buffers_most_recent_frame`.
- `test_video_frame_reaches_foreground_model` (fake `DuplexModel`
  captures `(audio, video | None)` tuples).
- `test_audio_without_video_passes_none`.
- `test_init_vision_flag_default_off`.

## Success criterion

```
pytest -k "vision_sidecar_buffers or video_frame_reaches or audio_without_video or init_vision_flag"
```

passes.

## §Out of scope

- Scoring + grounding real models.
- `DeicticDetector` wiring.
- Scene-change real models.

## §Cross-references

- `docs/roadmap-v0.1c-draft.md` Stage 2 milestone (this draft is also
  lost; cite via spec lines 70–120 in `docs/architecture-v0.1.md` for
  the Stage 2 contract).
- Live-loop milestone Task 8 (vision rendering).
- Issue #96 — `no_camera_memory` privacy gate.

## Top 2 risks

1. **`init_vision=True` interaction** with the `init_tts` no-op patch
   in `foreground_model_minicpm.py:128-133` and `as_duplex(...)`.
   Mitigated by flag-default-off + b200 pre-verification.
2. **GPU memory budget on b200** once the Anchor 5 follow-up adds a
   second vision-tower-bearing model.
