# Plan — VisionSidecar wiring (manual-test live pipeline)

## Status: **DRAFT** — converged 2026-05-15 (R1+R2 plan-critic loop; ready for fix-coder).

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

**Avoids:** MiniCPM-o re-running its vision tower on the same frame
for 10 consecutive audio chunks.

**Algorithm (consume-once, most-recent-wins):**

```
VisionSidecar internal state:
  _pending_frame: tuple[bytes, str] | None   # (frame_bytes, source_event_id), default None

ingest_frame_bytes(frame_bytes, event_id, ts_mono):
  if privacy_mode == "no_camera_memory":
      return  # privacy gate; no buffer, no event
  log vision_frame event with caused_by=[event_id]
  _pending_frame = (frame_bytes, vision_frame.event_id)
  # last-writer-wins — never grows beyond 1 frame

consume_pending_frame() -> tuple[bytes, str] | None:
  frame = _pending_frame
  _pending_frame = None    # consume-once
  return frame

has_pending_frame() -> bool:
  return _pending_frame is not None
```

**Determinism scope (invariants #5 + #6):** the policy layer's
bit-exact replay (invariant #5) is unaffected because video frames
flow only into the foreground model, never into `SpeakPolicy.decide()`.
The foreground proposal path is held to behavioral tolerance (invariant
#6), not bit-exact. Replay feeds `raw_video_frame` and
`raw_audio_chunk` events in their original `seq_no` order to the
orchestrator's input ports; `_pending_frame` updates whenever a video
frame is ingested between audio-chunk consumptions. What is required:
no video frame fed to the foreground twice and no frame silently lost
(the new unit test asserts both).

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

**Privacy gate:** the **first line** of `ingest_frame_bytes()` is
`if self._privacy_mode == "no_camera_memory": return` (no event
emitted, no buffer write). The **first line** of
`consume_pending_frame()` is
`if self._privacy_mode == "no_camera_memory": return None` (defensive;
the buffer should already be empty, but a mid-session privacy_mode
transition could leave a stale frame — see OQ-3a). Issue #96's
`no_camera_memory` contract test verifies both gates.

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
- **OQ-3a.** Mid-session privacy_mode transition. What happens when
  `privacy_mode` flips from `default` to `no_camera_memory` mid-session
  with a buffered frame already held? Lean: `_pending_frame` is cleared
  by an explicit `on_privacy_mode_change(new_mode)` method, called by
  whatever component owns the privacy mode (orchestrator? live_pipeline?).
  The defensive guard in `consume_pending_frame()` is the second line
  of defense.
- **OQ-4.** Max frame age — lean NO max-age check at v0.1f.
- **OQ-5.** v0.1c contract test coupling — lean: existing deictic
  fixtures use scripted frames, not live capture; should not break.

## Implementation sketch (12 bullets)

1. Extend `VisionSidecar` from its current stub.
2. Wire `raw_video_frame` ingest → `VisionSidecar.ingest_frame_bytes()`.
3. Update `live_pipeline.py` factory to construct one `VisionSidecar`
   per session.
4. Extend `StreamingRealtimeOrchestrator.__init__` to accept
   `vision_sidecar: VisionSidecar | None = None` (keyword-only). Store
   on `self._vision_sidecar`. In `_bounded_frame_gen`
   (`realtime_orchestrator.py:555-582`), replace both
   `yield frame_bytes, None` sites with
   `yield frame_bytes, self._consume_video_or_none()` where
   `_consume_video_or_none()` returns
   `self._vision_sidecar.consume_pending_frame()[0] if self._vision_sidecar else None`.
   Default `None` preserves backward compat.
5. Update `MiniCPMStreamingModel.infer_stream` (line ~185) so the
   unpack is `async for audio_bytes, video_bytes in frame_iter:`
   (rename `_video` → `video_bytes`). Audio path keeps existing
   behavior; this is the prep rename.
5a. (b200-only, manual verification) Wire `video_bytes` into the
    vision-prefill path. Pseudocode:
    ```python
    if video_bytes is not None:
        duplex.streaming_prefill(image=<decoded frame>)
    ```
    Success criterion: a single b200 run with `--enable-vision` shows
    the model's proposal text references a held-up object. File a
    follow-up issue if MiniCPM's streaming-prefill API differs.

> **Risk:** If `streaming_prefill(image=...)` API is not the right
> entry point on b200's MiniCPM build, sketch 5a needs a fix-coder
> revision after model-card verification.
6. Add `--enable-vision` CLI flag (default OFF) to server entrypoint.
7. Extend the startup banner to report `vision_enabled` state.
8. Extend `/healthz` to report `vision_enabled`, `last_frame_seq`,
   `frames_buffered`.
9. Wire the privacy-gate path (`no_camera_memory`) through
   `VisionSidecar.ingest_frame_bytes()`.
10. Emit `vision_frame` event from `VisionSidecar.ingest_frame_bytes()`
    with `caused_by=[raw_video_frame.event_id]`,
    `payload_kind="raw_video"`, `sensitivity="sensitive"`,
    `retention_policy_id="raw_media_default_300s"` (mirror existing
    `raw_video_frame` policy from `input_ingest.py`). Stage 0 contract
    test (orphan-event check) closes the DAG because
    `vision_frame.caused_by` cites the upstream `raw_video_frame.event_id`.
    The handbook already promises a `vision_frame` row in the display
    panel (`docs/manual-test-handbook.md:145`).
11. Document the operator flag in `docs/manual-test-handbook.md`.

## Test plan (5 contract tests)

- `test_vision_sidecar_buffers_most_recent_frame`.
- `test_video_frame_reaches_foreground_model` (fake `DuplexModel`
  captures `(audio, video | None)` tuples).
- `test_audio_without_video_passes_none`.
- `test_init_vision_flag_default_off`.
- `test_video_frame_consumed_once_then_buffer_empty`: ingest one frame;
  call `consume_pending_frame()` and assert non-None; call again
  immediately and assert `None`. Asserts the consume-once invariant
  from Anchor 2's algorithm.

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

1. **`init_vision=True` interacts with the existing `init_tts` no-op
   patch.** Mitigated by flag-default-off + explicit b200
   pre-verification, which must pass ALL THREE checks before the PR
   can merge:

   1. `AutoModel.from_pretrained(..., init_vision=True, init_audio=True, init_tts=False)`
      loads without `ImportError` in the canonical venv
      (`/raid/yid042/venvs/companion-harness`). Specifically: no
      `torchaudio` chain triggered by the vision tower.
   2. `as_duplex(generate_audio=False)` succeeds with the existing
      `init_tts`-patch in place (lines 128–133 of
      `foreground_model_minicpm.py`).
   3. `streaming_prefill(image=<sample JPEG decoded to PIL.Image>)`
      invokes the vision tower on at least one fixture frame and
      returns without raising.

   If any of (1)–(3) fails, this plan must be revised before
   implementation begins — sketch 5a needs a different prefill entry
   point (possibly `chat(image=...)` style for the duplex path; verify
   against the MiniCPM-o model card).

   Record VRAM before/after `init_vision=True` in the PR description
   (current baseline ~28GB with vision off; new baseline ~?GB).
2. **GPU memory budget on b200** once the Anchor 5 follow-up adds a
   second vision-tower-bearing model.
