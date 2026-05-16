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

**Primary checkpoint:** `pyannote/speaker-diarization-3.1` (gated — HuggingFace user agreement required).
**Fallback if gated access unavailable:** `pyannote/speaker-diarization-3.0` (released November 2023, ungated). The 3.0 → 3.1 API is identical per pyannote release notes; if a delta appears at impl time, a conditional load in `PyannoteDiarizationAdapter._ensure_loaded()` handles it.

> **OQ (impl-time):** Verify 3.0 / 3.1 API parity before T2 begins; if they diverge, an additional adapter class may be needed.

**Rejected:**
- NeMo speaker diarization (NVIDIA): heavier dependency footprint, requires CUDA, weights need access agreement.
- WhisperX speaker labels: tied to Whisper-large; we use whisper-tiny on the realtime path.

### Anchor 2 — Streaming-first API

`DiarizationAdapter.process_chunk(audio_bytes, ts_mono_ms, muted) -> DiarizationFrame` where `DiarizationFrame` carries:
- `speaker_id: str | None` (e.g., `"SPEAKER_00"`, `"SPEAKER_01"`, `None` = silence)
- `confidence: float`
- `is_new_speaker: bool` (first time this speaker_id was emitted in the session)

The `muted: bool` parameter is operator-controlled via the orchestrator: set `muted=True` from `assistant_audio_buffer_queued` to `assistant_audio_buffer_flushed + 300ms` (300ms tail to absorb playback propagation). When `muted=True`, the adapter skips diarization inference for that chunk and returns `DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)`. T2 implementation must honor this parameter.

Stateful: the adapter maintains a speaker-embedding registry per session and assigns stable `speaker_id`s across the session.

#### Event provenance

- New event type `diarization_frame_produced` emitted per non-trivial `DiarizationFrame` (skip when `speaker_id is None` to avoid silence-event spam).
- `caused_by=[raw_audio_chunk.event_id]` for the chunk that drove the frame.
- `retention_policy_id="signal_default_30d"`.
- `payload_inline` carries `speaker_id`, `confidence`, `is_new_speaker`.

### Anchor 3 — `current_speaker_id` field on `PolicyInputs`

New nullable field. Policy logic v0.1k uses it only as a TIE-BREAKER:
- If wake-word present → addressed (no change).
- If `current_speaker_id is not None` and it's the same speaker that wake-worded earlier in the session → addressed (continuity).
- Else → social_mode fallback (current behavior).

POLICY_VERSION bumps from the current `main` value to `v0.1k` because the policy rule changes (replay-affecting).

> **Note for implementers:** The current `main` POLICY_VERSION must be looked up at implementation time — do not hardcode the source version in this plan; v0.1g/h/i/j are all draft milestones whose POLICY_VERSION bumps may or may not be merged when this PR lands.

### Anchor 4 — Real backend uses GPU when available, CPU fallback

pyannote runs fine on CPU for short chunks (~50ms latency per 2s chunk). GPU optional but speeds up batch backfill (Phase C recordings).

Default CPU. `PyannoteDiarizationAdapter()` auto-detects CUDA in its constructor.

## Open questions (leans)

- **OQ-1**: Speaker registry persistence across sessions? **Lean**: NO at first version — fresh registry per session. Cross-session speaker recognition is a v0.1l+ concern. **Documented limitation:** a returning user across sessions gets a new `speaker_id` (e.g. `SPEAKER_00` in both). Tests asserting `is_new_speaker=False` across sessions will fail — this is expected per this lean. Cross-session persistence is v0.1l+ scope.
- **OQ-2**: Maximum speakers per session? **Lean**: cap at 4 (configurable); beyond that, label as `SPEAKER_OTHER`.
- **OQ-3**: How to handle the agent's own TTS being captured? **Lean**: use `assistant_audio_buffer_queued..assistant_audio_buffer_flushed` event stream as a mute-window — don't diarize during agent speech. Verify pyannote correctly identifies it as "non-foreground" when not muted.
- **OQ-4**: Real-time inference budget? **Lean**: 50ms p95 per 2s chunk. If exceeded, log `signal_producer_fallback` with reason `diarization_latency_exceeded`, fall through to mechanical social_mode. The `signal_producer_fallback` payload for this case: `{producer: 'diarization', reason: 'latency_exceeded', latency_ms_p95: <value>}`. This extends the existing `signal_producer_fallback` schema.
- **OQ-5**: `pyannote/speaker-diarization-3.1` requires HuggingFace user agreement (gated model). **Lean**: include in setup instructions; if unavailable, use the fallback `pyannote/speaker-diarization-3.0` (released November 2023, ungated). Do not use the unsuffixed `pyannote/speaker-diarization` — that resolves to 2.x.

## Tasks (sketch, 7 tasks)

### Wave 1 — Protocol + null stub
- **T1**: `DiarizationAdapter` Protocol + `_NullDiarizationAdapter` (returns `DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)` — Protocol-conformant, not raw tuple). Add to `companion_harness/diarization_adapter.py`.

### Wave 2 — Concrete pyannote adapter
- **T2**: `PyannoteDiarizationAdapter` implementing streaming inference. Embedding registry per session. Audio mute-window during agent TTS playback (subscribe to `assistant_audio_buffer_*` events).

### Wave 3 — Policy + addressing wiring
- **T3**: Add `current_speaker_id: str | None` to `PolicyInputs`. Default None preserves backward compat. When wake-word confirms a speaker, emit a `speaker_continuity_anchor` event with payload `{speaker_id, wake_word_event_id}` and `retention_policy_id="signal_default_30d"`. The policy layer's tie-breaker reads from this event sequence in `PolicyInputs`, NOT from an in-memory registry. Replay reconstructs the anchor list deterministically from the event log.
- **T4**: Extend `SpeakPolicy.decide()` with speaker-continuity tie-breaker (Anchor 3). POLICY_VERSION bump from the current `main` value to `v0.1k`. Update all `policy_version`-pinning fixtures.

> **Note:** Speaker-continuity integration with `MiniCPMAddressingClassifier` is deferred until issue #157 (libcudart blocker) is resolved. In the meantime, the speaker-continuity tie-breaker lives in `derive_user_addressed_agent` (which already gets `current_speaker_id` per T3).

### Wave 4 — Live pipeline wiring
- **T6**: `--enable-diarization` flag in `manual_test_console/server.py`. Construct `PyannoteDiarizationAdapter()` per-session when on (adapter auto-detects CUDA in its constructor). Plumb `DiarizationFrame.speaker_id` into the `PolicyInputs` builder.

### Wave 5 — Contract tests
- **T7**:
  - `test_pyannote_diarization_returns_stable_speaker_ids`
  - `test_diarization_mute_window_during_agent_speech`
  - `test_policy_speaker_continuity_tie_breaker`
  - `test_policy_replay_exact_v0_1k` (POLICY_VERSION bump)
  - `test_diarization_latency_p95_under_50ms` (perf gate)
  - `test_diarization_adapter_satisfies_protocol`
  - `test_pyannote_loads_without_error`
  - `test_diarization_events_have_caused_by` (asserts orphan-event check; Stage 0 invariant #1)

Wave 5 completion satisfies the diarization prerequisite for Eval Phase C.

## Numeric gates

### v0.1k ship criteria (measurable now)

| Metric | Gate |
|---|---|
| `diarization_latency_ms_p95` | < 50ms |
| `test_diarization_adapter_satisfies_protocol` | pass |
| `test_pyannote_loads_without_error` | pass |

### Advisory metrics (gated in Eval Phase C when ground-truth corpus exists)

| Metric | Target |
|---|---|
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

## Coordination

Eval Phase C currently has a `social_mode` stub from the v0.1j Task 9 work. This plan REPLACES that stub with real diarization; the v0.1j Task 9 stub is retired when this PR merges. Update `roadmap-eval-draft.md` Phase C prerequisites in this PR as well.

## Cross-references

- `docs/eval-subsystem-spec.md` — Phase C (live examiner) requires diarization.
- `docs/architecture-v0.1.md` — addressing classifier discussion.
- pyannote.audio docs: https://github.com/pyannote/pyannote-audio
- `companion_harness/addressing_classifier.py` — current mechanical social_mode fallback.
