# Plan — Real diarization adapter — DRAFT

## Status

**DRAFT — 2026-05-15.** Replaces the mechanical `social_mode` derivation in the addressing classifier with a real diarization signal. Unblocks Eval Phase C (live examiner).

## Why

Today `social_mode` is derived mechanically: if no wake-word, assume `solo` (single human in session). This works for the lead's one-person manual tests but breaks immediately when:
- Two people are talking and only one is addressing the agent.
- The agent's own TTS output gets re-captured by the mic (acoustic feedback loop misidentified as a second speaker).
- A third party speaks in the room.

Diarization (who-is-speaking-when) is the prerequisite for:
- Better addressing classifier (don't respond to non-addressee utterances).
- `guest_present` privacy gate auto-activation.
- Eval Phase C (live examiner) — needs to attribute utterances to specific speakers across long recordings.

## Goal

Add a `DiarizationAdapter` Protocol + a real backend that segments incoming audio into speaker-attributed regions. Plumb the resulting `current_speaker_id` into `PolicyInputs` so policy + addressing classifier can use it.

## Anchor decisions — non-transient

### Anchor 1 — Model choice: pyannote.audio primary

Use **`pyannote/speaker-diarization-3.1`** via HuggingFace pyannote.audio. Justification:
- Industry-standard, well-maintained, off-the-shelf weights.
- ~30MB segmentation model + ~30MB embedding model. Fits CPU.
- Streaming-capable via online inference recipe (process audio in 2-3s chunks with overlap).
- Apache 2.0 license.

**Rejected:**
- NeMo speaker diarization (NVIDIA): heavier dependency footprint, requires CUDA, weights need access agreement.
- WhisperX speaker labels: tied to Whisper-large; we use whisper-tiny on the realtime path.

### Anchor 2 — Streaming-first API

`DiarizationAdapter.process_chunk(audio_bytes, ts_mono_ms) -> DiarizationFrame` where `DiarizationFrame` carries:
- `speaker_id: str | None` (e.g., `"SPEAKER_00"`, `"SPEAKER_01"`, `None` = silence)
- `confidence: float`
- `is_new_speaker: bool` (first time this speaker_id was emitted in the session)

Stateful: the adapter maintains a speaker-embedding registry per session and assigns stable `speaker_id`s across the session.

### Anchor 3 — `current_speaker_id` field on `PolicyInputs`

New nullable field. Policy logic v0.1k uses it only as a TIE-BREAKER:
- If wake-word present → addressed (no change).
- If `current_speaker_id is not None` and it's the same speaker that wake-worded earlier in the session → addressed (continuity).
- Else → social_mode fallback (current behavior).

POLICY_VERSION bumps `v0.1j → v0.1k` because the policy rule changes (replay-affecting).

### Anchor 4 — Real backend uses GPU when available, CPU fallback

pyannote runs fine on CPU for short chunks (~50ms latency per 2s chunk). GPU optional but speeds up batch backfill (Phase C recordings).

Default CPU. `--diarization-gpu` flag enables CUDA.

## Open questions (leans)

- **OQ-1**: Speaker registry persistence across sessions? **Lean**: NO at first version — fresh registry per session. Cross-session speaker recognition is a v0.1l+ concern.
- **OQ-2**: Maximum speakers per session? **Lean**: cap at 4 (configurable); beyond that, label as `SPEAKER_OTHER`.
- **OQ-3**: How to handle the agent's own TTS being captured? **Lean**: use `audio_out_chunks_sent` event stream as a mute-window — don't diarize during agent speech. Verify pyannote correctly identifies it as "non-foreground" when not muted.
- **OQ-4**: Real-time inference budget? **Lean**: 50ms p95 per 2s chunk. If exceeded, log `signal_producer_fallback` with reason `diarization_latency_exceeded`, fall through to mechanical social_mode.
- **OQ-5**: pyannote `pyannote/speaker-diarization-3.1` requires HuggingFace user agreement (gated model). **Lean**: include in setup instructions; if unavailable, use the older `pyannote/speaker-diarization` (no gate).

## Tasks (sketch, 7 tasks)

### Wave 1 — Protocol + null stub
- **T1**: `DiarizationAdapter` Protocol + `_NullDiarizationAdapter` (returns `(None, 0.0, False)`). Add to `companion_harness/diarization_adapter.py`.

### Wave 2 — Concrete pyannote adapter
- **T2**: `PyannoteDiarizationAdapter` implementing streaming inference. Embedding registry per session. Audio mute-window during agent TTS playback (subscribe to `assistant_audio_buffer_*` events).

### Wave 3 — Policy + addressing wiring
- **T3**: Add `current_speaker_id: str | None` to `PolicyInputs`. Default None preserves backward compat.
- **T4**: Extend `SpeakPolicy.decide()` with speaker-continuity tie-breaker (Anchor 3). POLICY_VERSION bump `v0.1j → v0.1k`. Update all `policy_version`-pinning fixtures.
- **T5**: Extend `MiniCPMAddressingClassifier` to consume `current_speaker_id` as an additional signal in the yes/no prompt.

### Wave 4 — Live pipeline wiring
- **T6**: `--enable-diarization` flag in `manual_test_console/server.py`. Construct per-session pyannote adapter when on. Plumb `DiarizationFrame.speaker_id` into the `PolicyInputs` builder.

### Wave 5 — Contract tests + Phase C unblock
- **T7**:
  - `test_pyannote_diarization_returns_stable_speaker_ids`
  - `test_diarization_mute_window_during_agent_speech`
  - `test_policy_speaker_continuity_tie_breaker`
  - `test_policy_replay_exact_v0_1k` (POLICY_VERSION bump)
  - `test_diarization_latency_p95_under_50ms` (perf gate)

## Numeric gates

| Metric | Gate |
|---|---|
| `diarization_latency_ms_p95` | < 50ms |
| `diarization_speaker_continuity_addressing_accuracy` | > 0.85 (vs ground-truth on Eval Phase C cases) |
| `diarization_false_speaker_change_rate` | < 0.05 |

## Risks

1. **Pyannote gated weights**: HF user agreement required. Mitigation: document in `remote-dev.md`; fall back to ungated v3.0 model.
2. **Acoustic feedback (TTS re-captured)**: pyannote may register agent voice as a new speaker. Mitigation: mute-window during `assistant_audio_buffer_queued..assistant_audio_buffer_flushed`. Verify in contract test.
3. **POLICY_VERSION bump cascade**: every fixture pinning `policy_version` must update. Mitigation: schema-test that policy_version is sourced from a single constant + replay-fixture migration in same PR.

## Out of scope

- Cross-session speaker recognition (v0.1l+).
- Voiceprint-based authentication.
- Speaker-attributed event payload retention (separate retention-policy concern).

## Cross-references

- `docs/eval-subsystem-spec.md` — Phase C (live examiner) requires diarization.
- `docs/architecture-v0.1.md` — addressing classifier discussion.
- pyannote.audio docs: https://github.com/pyannote/pyannote-audio
- `companion_harness/addressing_classifier.py` — current mechanical social_mode fallback.
