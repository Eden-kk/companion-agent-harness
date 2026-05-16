# Plan — Streaming-speculative proposer + integrated barge-in cancellation (Path B)

## Status: **DRAFT** — drafted 2026-05-16.

| Field | Value |
|---|---|
| Target milestone | **v0.3** (argued in §0 below; not v0.2.1) |
| Pinned success criterion | A live console run with `--streaming-speculative` answers a direct question with p50 EOU→first-TTS-chunk < 800 ms, allows user barge-in to cut active TTS within 500 ms of speech onset, and the existing Tier-B replay match-rate gate (1.0) still passes against the recorded event log. |
| Dependency PRs | #301, #305, #306, #308, #315, #316, #317, #318, #321, #322 (all merged); F0a, F0b, F0c, F0d, F1b, F4, G1, R2-3 fixes assumed in place. HEAD at plan start: `0b49d54`. |
| Depends on prior plan | `docs/basic-stack-design-2026-05-16.md` §4 (basic-stack risks this plan addresses). |
| Defers / does not regress | All 19/19 v0.2 readiness gates; `POLICY_VERSION = v0.2-final` not bumped. |
| Sign-off table | __ plan-critic (READY)  __ Codex review  __ operator approval (`/plan-review` convergence) |

**Argument for v0.3 over v0.2.1:** v0.2.1 is reserved (per `basic-stack-design-2026-05-16.md` §8) for F2 line-cap, G1/R2-3 sampled-event tombstones, and F0a driver re-verification — all small surgical fixes against the existing turn-batched code path. Path B is a structural change to T3/T4 and the proposer lifecycle, plus a new barge-in mechanism that crosses three modules. Squeezing it into the .1 patch milestone would blur the line between bug-fix and feature work and force regression-testing the entire v0.2 stack on every interim sub-PR. Slot it as v0.3 alongside the F1 voice-mode-addressing redesign already named there; the two share replay-determinism concerns.

---

## §1 — Background and goal

### What turn-batched does today

Today (HEAD `0b49d54`), T3 (`_foreground_stream_task`, `realtime_orchestrator.py:963-986`) is *batched per turn*: it waits on `_batch_open_event` (set by T2 at `:956`), drains the inter-batch frame queue (F0b fix, `:973-977`), then runs `_foreground_model.process_stream(frame_iter, ...)` whose `frame_iter` terminates when `_batch_close_event` fires (`_bounded_frame_gen`, `:1003-1030`). The proposer is therefore **cold-started per turn**; first-token latency is the proposer's wall-clock from `_batch_open_event` until `proposal_buffer.append(...)` at `:985`. The F0c sweep (`manual-test-findings-2026-05-16.md` ll. 142-148) measured this as 100% timeout on rapid back-to-back turns at the original 80 ms window, partial recovery at 200 ms, full recovery only at 600+ ms. EOU→first-TTS-chunk latency math: VAD silence-onset (~200 ms) + ASR (~80 ms) + addressing (~50 ms) + policy (<1 ms) + proposer first-token (200-800 ms cold) + Kokoro first chunk (~1-3 s). Path A (turn-batched) is structurally bounded below by the proposer cold-start.

### What streaming-speculative (Path B) gives us

`MiniCPMStreamingModel.infer_stream` (`foreground_model_minicpm.py:226-317`) is already an async generator that accumulates 1-second audio chunks and emits a `ThinkerProposal` whenever `streaming_generate` returns `is_listen=False`. The model's internal `_last_is_listen` flag (`:139`) is the same signal already exposed via `MiniCPMNativeDuplexEouSource` (`native_duplex_eou.py:48-76`). Path B holds **one** `infer_stream` invocation alive across many turns, lets the model speculatively emit tokens as the user is still speaking, and **commits or discards** the buffered tokens at the EOU policy boundary instead of paying a per-turn cold start. The result: first-TTS-chunk latency becomes dominated by Kokoro warm-up (~200 ms first chunk) once we also do §3.1's sub-chunking — the proposer is no longer on the critical path.

### Why integrated barge-in is in scope

The barge-in test reference `#aebe6` (basic-stack-design-2026-05-16.md §4 Concern 6) found that the cancellation predicate at `realtime_orchestrator.py:1287-1293` — `is_barge_in_trigger` requires `is_playing AND NOT is_synthesizing` — **never opens** for the current Kokoro adapter. `KokoroTtsAdapter.synthesize` (`tts_kokoro.py:119-140`) yields one Kokoro-internal-phoneme-batch's worth of audio per `create_stream` step; observed empirically as a single ~3 s chunk per utterance. `AudioOutputController.play` (`audio_output_controller.py:142-171`) flips `_synthesizing = False` only at the first chunk (`:158`). For short utterances, the first chunk **is** the whole utterance, so `is_synthesizing=False` and `is_playing=True` coincide for ~5 ms, not the 200+ ms barge-in needs. Sub-chunking Kokoro output (§3.1) is the prerequisite that makes the existing barge-in predicate usable; Path B's continuous proposer makes barge-in *necessary* (because the model keeps speculating during playback, so a clean cancellation must also drop the in-flight speculation, not just the audio).

---

## §2 — Architectural overview

### Side-by-side T1–T4

| Component | Today (turn-batched, Path A) | Proposed (streaming-speculative, Path B) |
|---|---|---|
| T1 `_detector_fanout_task` | VAD + SmartTurn + Backchannel; emits `vad_turn_signal` on EOU; forwards `_VadOnsetFrame` to T2 for barge-in. | Unchanged. (VAD stays as safety-net EOU + barge-in onset producer.) |
| T2 `_policy_gate_task` | Per-EOU: ASR → addressing → policy → opens batch via `_batch_open_event`. | Per-EOU: ASR → addressing → policy → **commit_or_discard signal to T4**; batch_open/close events deprecated in Path B (but kept compiled for Path A). |
| T3 `_foreground_stream_task` | Per-batch: `await _batch_open_event`; drain stale frames; run `infer_stream(frame_iter)` whose `frame_iter` ends when `_batch_close_event` fires. | **Single lifetime invocation**: `infer_stream(continuous_frame_iter)` over the whole session; every yielded proposal lands in a *ring buffer* (not the per-batch `proposal_buffer` list). |
| T4 `_synthesis_dispatch_task` | Per-EOU: await policy future; close batch; grace-window on `proposal_buffer`; synthesize. | Per-EOU: await policy future; if `silence` → discard ring contents tagged with the un-committed range; if `full_response` → snapshot ring → Kokoro. **No grace window** — tokens already exist by EOU time. |
| New: EOU producer | VAD/SmartTurn primary; `MiniCPMNativeDuplexEouSource` is `_NullNativeDuplexEouSource` by default (`realtime_orchestrator.py:317-319`). | `MiniCPMNativeDuplexEouSource` promoted to **primary** EOU producer when `infer_stream` is live; VAD stays as the safety-net for both EOU and barge-in onset. |
| New: barge-in cancellation | `_fire_barge_in` (`:1295-1337`) calls `request_stop` → bounded `cancel_generation`. Predicate gated by `not is_synthesizing` (never opens for short utterances). | Same `_fire_barge_in` path, but: (a) Kokoro sub-chunked so `is_synthesizing` falls promptly (§3.1), (b) cancellation also discards the un-committed ring tail (`barge_in_discards_buffered_tokens`), (c) MiniCPM-o response-mode state is reset without resetting the audio KV cache (§3.5). |
| New: KV-cache reset | Internal: `audio_past_key_values length 1502 exceed 1500, reset.` fires *whenever* the cap is hit — including mid-response, which is racy. | Coordinated: reset only when `_last_is_listen=True` AND no commit pending (§3.5); emits `model_context_window_reset` event for replay. |

### ASCII data-flow (Path B)

```
              raw_audio_chunk events
                       │
                       ▼
              _audio_tee_task ──┬── _tee_to_detectors ──► T1 (VAD+SmartTurn+BC)
                                │                            │
                                │                            │ TurnSignal / _VadOnsetFrame
                                │                            ▼
                                │                          T2 _t2_inbox
                                │                            │
                                │              ┌─────────────┴─────────────┐
                                │              │ onset?                     │ EOU?
                                │              ▼                            ▼
                                │     _fire_barge_in              ASR + addressing + policy
                                │              │                            │
                                │              │                            ▼
                                │              │                  commit_or_discard signal
                                │              │                            │
                                │              ▼                            ▼
                                │     AudioOutputController.        T4: snapshot ring → TTS
                                │     request_stop +                       │
                                │     cancel_generation                    │
                                │     +                                    │
                                │     discard_ring_tail                    │
                                │                                          ▼
                                │                                 KokoroTtsAdapter
                                └── _tee_to_foreground ──► T3 (continuous lifetime)
                                                            │
                                              MiniCPMStreamingModel.infer_stream
                                              (one invocation; yields tokens speculatively
                                               whenever model decides to speak; updates
                                               _last_is_listen on every chunk)
                                                            │
                                                            ▼
                                                _proposal_ring (capped, monotonic seq)
                                                    │
                                                    │  read by MiniCPMNativeDuplexEouSource
                                                    │  → primary EOU producer for T2
                                                    │  read by T4 at commit_or_discard
                                                    │  read by barge-in: discard tail
                                                    │  reset coordinated by §3.5
```

The contract: T3 *writes* the ring; T4 + barge-in *commit_or_discard* by snapshotting an index and clearing up to (or past) it; the ring's append-seq is the deterministic identifier for any later audit query.

---

## §3 — Sub-plans

Each sub-plan is independently dispatchable. Files are absolute relative to repo root.

### 3.1 — Kokoro sub-chunking (prerequisite, isolated)

**Goal.** Make `KokoroTtsAdapter.synthesize` emit ≤200 ms PCM chunks so `AudioOutputController._synthesizing` falls within the barge-in window for any utterance length.

**Files touched.**
- `companion_harness/tts_kokoro.py` — add slicing inside the `async for samples, _sr in self._kokoro.create_stream(...)` loop at `:131-140`. After the float32→int16 conversion (`:139`), if the chunk exceeds the byte budget for 200 ms at 24 kHz mono 16-bit (9600 bytes), split and yield in slices ≤9600 bytes. Constant `_MAX_CHUNK_BYTES = 9600`. No new dependencies. No change to the public Protocol.

**Acceptance test.** `tests/test_tts_kokoro_subchunking.py::test_kokoro_yields_subchunks_under_200ms` — synthesize a 5-second test phrase with a recorded-Kokoro fake (existing fixture in `tests/fixtures/`), assert every yielded chunk satisfies `len(chunk) <= 9600` and the concatenation is byte-identical to the un-sliced output.

**Dependencies.** None. Can ship today against `main`.

**Estimated LOC.** ~30 LOC in `tts_kokoro.py` + ~40 LOC test.

**Risk.** Slicing inside a `float32→int16` step is byte-safe (PCM has no inter-sample state); risk is zero for content fidelity. Risk for ordering: `yield` boundaries inside the inner loop must respect `await self._sink(chunk)` cadence in `AudioOutputController.play` (`:169`) — already async, no additional buffering needed.

---

### 3.2 — `_dropped_before_enqueue` root-cause (parallel; non-blocking)

**Goal.** Identify the second source of `log_drop_or_degrade` events (148-157 drops/run survived #305's ring-size raise and #321's display-broker rework). Without this, Path B's instrumentation will trip the same backpressure under heavier event volume.

**Files touched.** Read-only investigation pass, then targeted fix:
- `companion_harness/realtime_orchestrator.py:420-421` — `_drop_oldest_put` for both tees uses `["_dropped_before_enqueue"]` as the *caused_by*; the literal string "_dropped_before_enqueue" is **a fake event_id** that no event actually has. The drops are reported but the audit chain still cannot find the upstream cause.
- `companion_harness/event_logger.py` — confirm the second drop site (likely the per-subscriber WS broker queue, distinct from the main ring).

**Acceptance test.** `tests/test_dropped_before_enqueue_audit.py::test_dropped_before_enqueue_source_identified` — run a synthetic 30-s session at 100 frames/s, assert every `log_drop_or_degrade` event has a *non-sentinel* `caused_by[]` (the offending upstream event_id), and total drops < 5 under the synthetic load that previously produced ~150.

**Dependencies.** None. Can run in parallel with 3.1.

**Estimated LOC.** ~80 LOC fix + ~60 LOC test (after the debugger pass picks a fix shape; the shape is one of: rewrite `_drop_oldest_put` to capture the upstream `caused_by` arg, OR thread the chunk_event_id into the drop event by closure).

---

### 3.3 — Path B core: continuous consumption + native-duplex EOU + commit/discard

**Goal.** Replace the per-batch T3 lifetime with one continuous `infer_stream`; route MiniCPM's `_last_is_listen=False` as the primary EOU producer; T4 commits or discards the un-acked ring tail instead of waiting for a per-batch proposal.

**Files touched.**

- `companion_harness/realtime_orchestrator.py`:
  - **Constructor.** Add `use_streaming_speculative: bool = False` parameter; gate all new behavior on this flag for backward-compat. Add `self._proposal_ring: list[tuple[int, ThinkerProposal]] = []` and `self._proposal_ring_committed_seq: int = 0` (last-committed monotonic index).
  - **T3 (`_foreground_stream_task`, `:963-986`).** When flag is ON: do **not** await `_batch_open_event`; do **not** drain frames; instead loop forever consuming `_tee_to_foreground` into a single `continuous_frame_gen` and call `infer_stream(continuous_frame_gen, caused_by=[orchestrator_started_event_id], context_items=())` exactly once at task start. Every yielded proposal is appended to `_proposal_ring` with a monotonically incremented seq. The first-proposal event flag (`_first_proposal_event`) is no longer set in Path B; T4 reads the ring directly.
  - **T2 (`_policy_gate_task`, `:481-961`).** When flag is ON: the existing call to `_native_duplex_eou_source.get_eou_signal()` at `:519` is already correct — Path B just requires `MiniCPMNativeDuplexEouSource` (not `_NullNativeDuplexEouSource`) to be injected. The `signal_producer_fallback` emission at `:526-541` becomes the safety-net path when MiniCPM's `_last_is_listen` is stuck (e.g., long monologue from user). VAD signals continue arriving on `_t2_inbox` and still drive EOU when native_duplex is silent.
  - **T4 (`_synthesis_dispatch_task`, `:1032-1149`).** When flag is ON: remove the grace window at `:1088-1116` (proposals already exist). On `decision.action_type == "silence"` (`:1047`), emit a new `commit_or_discard` event with `payload_inline={"committed": False, "discarded_token_count": len(_proposal_ring) - _proposal_ring_committed_seq, "signal_evt_id": signal_evt_id}` and clear the ring tail past `_proposal_ring_committed_seq`. On `full_response` (current `:1118-1149`), emit `commit_or_discard` with `committed=True`, snapshot the ring tail, advance `_proposal_ring_committed_seq`, and dispatch Kokoro as today. The `_batch_open_event` / `_batch_close_event` machinery is dead code under Path B but stays compiled (Path A still uses it when flag OFF).
  - **`MiniCPMStreamingModel.infer_stream` lifetime.** The current implementation (`:226-317`) takes `caused_by: list[str]` and `context_items: tuple[MemoryItem, ...] = ()` at invocation time. For Path B, `caused_by` becomes the static `orchestrator_started_event_id`; context_items must be injected dynamically *per turn*. Today `streaming_prefill(prefix_system_prompt=combined)` is called once inside `infer_stream._gen` at `:252` (the docstring at `:246` even says "MiniCPM-o does not support mid-session re-prepare; context is folded at first-call only"). **This is a real blocker** — Path B cannot honor per-turn memory retrieval without an upstream fix to MiniCPM-o's wrapper. Resolution for v0.3: ship Path B with **session-level context only** (the `set_context` items from the most recent EOU are folded by re-calling `prepare()` on a fresh `as_duplex` session, which means a ~50 ms reset on each turn). Cite this as a known limitation in §5 risks; full fix is a future ticket against MiniCPM-o.

- `companion_harness/foreground_model_minicpm.py`:
  - Add `infer_stream_continuous(frame_iter, caused_by, *, on_proposal: Callable[[ThinkerProposal], None]) -> AsyncIterator[None]` as a Path-B-specific entry that calls `on_proposal(p)` for each yielded proposal (rather than the caller iterating over the returned AsyncGenerator). This avoids the per-batch teardown semantics of the current `_gen()` and lets T3 keep a single `async for` over a never-terminating generator.
  - Add `reset_response_mode(self) -> None` that calls `self._duplex.reset_response_state()` (or the equivalent — check MiniCPM-o's API on b200) **without** clearing `audio_past_key_values`. Used by §3.4 barge-in.

- `manual_test_console/live_pipeline.py`:
  - When constructing the orchestrator, pass `native_duplex_eou_source=MiniCPMNativeDuplexEouSource(minicpm_streaming_model)` when the model is available AND the streaming-speculative flag is set (read from config_store or CLI).

**New events.**
- `proposer_token_buffered` (per ring append; `payload_inline={"ring_seq": int, "is_listen": bool, "text_preview": str[:32]}`). Emitted by `MiniCPMStreamingModel` via the existing `_emit_invocation` pattern (`:325-352`); subject_class="self", sensitivity="safe".
- `commit_or_discard` (one per policy decision under Path B). `payload_inline={"committed": bool, "discarded_token_count": int, "committed_token_count": int, "signal_evt_id": str, "policy_evt_id": str}`. Source: `streaming_realtime_orchestrator`.

**Acceptance tests.**
- `tests/test_streaming_speculative_continuous.py::test_continuous_proposer_keeps_tokens_across_turns` — feed a 20-second audio stream (two utterances of 5 s each, 5 s of silence between), assert `infer_stream` is invoked exactly once, `_proposal_ring` accumulates monotonically, and turn 2 sees proposals appended without a teardown event.
- `tests/test_streaming_speculative_continuous.py::test_policy_silence_discards_buffered_tokens` — script policy to return `silence`; assert `commit_or_discard` is emitted with `committed=False` and non-zero `discarded_token_count`, ring tail is cleared, `_proposal_ring_committed_seq` does not advance.
- `tests/test_streaming_speculative_continuous.py::test_policy_commit_dispatches_immediately_no_grace_window` — script policy to return `full_response`, ring already has 5 proposals at decision time; assert `tts_synthesis_started` fires within 50 ms of `policy_decision` (no 600 ms grace).

**Dependencies.** 3.1 (sub-chunking) is recommended-before so the barge-in test in 3.4 can pass against Path B output. 3.7 (flag) is hard prerequisite — this sub-plan is written *under* the flag.

**Estimated LOC.** ~400 LOC in orchestrator + ~80 LOC in foreground_model_minicpm + ~30 LOC in live_pipeline + ~200 LOC tests. Net core ≈ 600 LOC.

---

### 3.4 — Integrated barge-in

**Goal.** When VAD detects user speech onset while `is_playing=True`, hard-cancel the in-flight TTS within 500 ms, discard the un-committed tail of `_proposal_ring`, and reset MiniCPM-o's response-mode state without resetting the audio KV cache.

**Files touched.**

- `companion_harness/realtime_orchestrator.py`:
  - **`_fire_barge_in` (`:1295-1337`).** Today: calls `request_stop` → bounded shield wait → `cancel_generation`. Extend (Path B only): immediately *after* the existing cancel branch but *before* the `finally` cleanup, append: (a) snapshot the current `_proposal_ring_committed_seq`; (b) clear `_proposal_ring[_proposal_ring_committed_seq:]`; (c) call `self._foreground_model._model.reset_response_mode()` (Path B exposed in §3.3); (d) emit `barge_in_cancelled` with `payload_inline={"interrupted_utterance_event_id": gen_event_id, "chunks_played": int, "chunks_remaining": int, "ring_tail_discarded": int}`.
  - **`is_barge_in_trigger` (`:1287-1293`).** Today requires `not self._audio_output.is_synthesizing`. After §3.1 lands, `is_synthesizing` flips on the first ≤200 ms sub-chunk, so this predicate now actually opens for short utterances. No code change here; just an empirical un-blocking enabled by 3.1.
  - **Tracking `chunks_played` / `chunks_remaining`.** `AudioOutputController.play` (`audio_output_controller.py:142-171`) currently emits `assistant_audio_buffer_queued` per chunk (`:168`) but does not maintain a counter. Add `self._chunks_played: int = 0` to `AudioOutputController.__init__` (`:64-77`), reset to 0 in `start_generation` (`:83-94`), increment in `play` (`:167`). Expose `chunks_played` property. The chunks-remaining count comes from the not-yet-iterated portion of the async iterator and is **not knowable without buffering** — record `chunks_remaining: -1` (sentinel: unknown) and document in the payload_inline schema.

- `companion_harness/audio_output_controller.py`:
  - Add `chunks_played` counter as above.
  - No change to `request_stop` or `cancel_generation`; the existing primitives are sufficient.

**New events.**
- `barge_in_cancelled` (one per successful barge-in). `payload_inline={"interrupted_utterance_event_id": str, "chunks_played": int, "chunks_remaining": int, "ring_tail_discarded": int}`. Source: `streaming_realtime_orchestrator`. caused_by: `[onset_evt_id, gen_event_id]`.

**Acceptance tests.**
- `tests/test_barge_in_integrated.py::test_barge_in_cancels_active_tts_under_500ms` — record `tts_synthesis_started` timestamp T0; trigger VAD onset at T0+1500 ms via injected fake; assert `assistant_audio_stop_completed` fires by T0+2000 ms (i.e., barge-in path completes within 500 ms of onset).
- `tests/test_barge_in_integrated.py::test_barge_in_discards_buffered_tokens` — script proposer to append 5 tokens to the ring during playback; barge-in fires; assert `barge_in_cancelled` payload reports `ring_tail_discarded >= 1`, and after barge-in completes the ring tail is empty.
- `tests/test_barge_in_integrated.py::test_post_barge_in_new_utterance_starts_fresh` — barge-in fires; user says "stop, instead, tell me about X"; assert next `policy_decision` emits `full_response` and the resulting `commit_or_discard` event carries committed tokens whose preview text does NOT contain any token text from the discarded tail (i.e., MiniCPM's response-mode reset prevented continuation of the cancelled response).

**Dependencies.** 3.1 (chunking unblocks the predicate), 3.3 (ring + commit/discard semantics).

**Estimated LOC.** ~180 LOC in orchestrator + ~30 LOC in audio_output_controller + ~150 LOC tests. Net ≈ 250 LOC.

---

### 3.5 — KV cache windowed reset

**Goal.** Coordinate MiniCPM-o's internal `audio_past_key_values` cap (currently 1500-frame window) so it can never reset mid-response, which would corrupt the speculative tokens already in the ring.

**Files touched.**

- `companion_harness/foreground_model_minicpm.py`:
  - Wrap the `streaming_generate` call (`:258-266`) with a pre-check. The current MiniCPM-o internal log `audio_past_key_values length 1502 exceed 1500, reset.` indicates the model is auto-resetting. Add a pre-call check: if `len(self._duplex.audio_past_key_values) > 1400` AND `self._last_is_listen is True` AND `self._committing_in_flight is False` (new flag, set by T4 before commit, cleared after Kokoro task complete), call `self._duplex.reset_audio_past_key_values()` ourselves and emit a `model_context_window_reset` event via `_emit_invocation`-style helper. If the cap is hit while `is_listen=False` (mid-response), **do not reset** — let the buffered token already in flight finish, then reset on the next listen cycle.
  - Add `self._committing_in_flight: bool = False` plus setters callable from the orchestrator's T4 (`set_committing(True/False)`).

- `companion_harness/realtime_orchestrator.py`:
  - Around the Kokoro dispatch in `_synthesis_dispatch_task` (`:1124-1149`), bracket with `self._foreground_model._model.set_committing(True)` before `start_generation` and `set_committing(False)` in the `finally` block. Only active when Path B flag is ON.

**New events.**
- `model_context_window_reset` (one per reset). `payload_inline={"kv_length_before": int, "last_is_listen": bool, "trigger": "windowed_at_listen_boundary"}`. Source: `minicpm_streaming`.

**Acceptance tests.**
- `tests/test_kv_cache_windowed_reset.py::test_context_reset_never_mid_response` — drive a 60-s session crossing the 1500-frame cap multiple times, scripting the model fake to be in `is_listen=False` at the moment the cap is hit; assert zero `model_context_window_reset` events fire during `is_listen=False` windows; assert resets happen only at `is_listen=True` boundaries.
- `tests/test_kv_cache_windowed_reset.py::test_long_session_no_unbounded_memory_growth` — drive a 1-hour synthetic session (compressed clock); assert peak `len(audio_past_key_values)` stays under 1500 + 10% headroom across the session.

**Dependencies.** 3.3 (T4 needs to call `set_committing`).

**Estimated LOC.** ~80 LOC in foreground_model_minicpm + ~20 LOC in orchestrator + ~80 LOC tests. Net ≈ 150 LOC.

---

### 3.6 — Audit story for discarded speculations

**Goal.** Preserve invariant 5 (policy-layer replay must be bit-identical) when the proposer's lifecycle now produces tokens that almost-spoke but didn't.

**Argument for both options.**

| Option | Pro | Con | Recommendation |
|---|---|---|---|
| (a) Record everything — every `proposer_token_buffered` event includes full text payload via `payload_ref` to a blob. | Replayer can bit-reconstruct the discarded content; perfect forensic story. | Blob volume ≈ KB/s of speculative tokens during conversation; under hour-long sessions this is 10s of MB additional blob storage, plus the EventLogger ring pressure. Invariant 10 risk. | Avoid as default. |
| (b) Record decisions only — `commit_or_discard` carries `discarded_token_count` and `committed_token_count` (integers); `proposer_token_buffered` events carry only the ring_seq + is_listen + 32-char preview. | Minimal volume; replay reconstructs *that* tokens were discarded and *how many*, satisfying behavioral replay (invariant 6) but not byte-exact reconstruction of the discarded text. | Replay cannot byte-match a discarded token. **But: invariant 5 is about policy decisions being bit-identical, not about model proposals being bit-identical.** The policy decision is deterministic from `PolicyInputs`, which excludes the proposer's discarded tokens (proposer feeds T4, not T2). Replay match-rate is unaffected. | **Recommended default.** |
| (c) Hybrid: record-decisions-only by default + `--audit-speculations` debug flag that lights up option (a). | Operator can dial in full forensics for a specific session. | Code path divergence between modes — must be tested both ways. | Ship (b) + (c) together: (b) is the default; (c) is a `--audit-speculations` CLI flag on `manual_test_console.server` that, when set, also writes the speculative-token payloads to the blob store. |

**Files touched.**

- `companion_harness/realtime_orchestrator.py`:
  - `__init__`: add `audit_speculations: bool = False` parameter, store on `self._audit_speculations`.
  - In the `proposer_token_buffered` emission path (added in 3.3), when `_audit_speculations=True`, also write the proposal's `content` text to the blob store via `_store_payload(event_id, {"speculative_token_text": SensitiveField(value=p.content, ...)})` and set `payload_ref="orchestrator://{event_id}"`. Otherwise `payload_ref=None` and `payload_inline` carries only the 32-char preview.

- `manual_test_console/server.py`:
  - Add `--audit-speculations` argparse flag (default False); thread through `build_live_pipeline` into orchestrator constructor.

**Acceptance test.** `tests/test_replay_match_rate_path_b.py::test_replay_match_rate_holds_under_streaming` — record a 30-s Path B session; run the existing Tier-B replay machinery (`companion_harness/replay.py`) against the captured events; assert match_rate == 1.0 (same gate as today). Run twice: once with `--audit-speculations` off, once on; both must hit 1.0.

**Dependencies.** 3.3 (the events being audited are introduced there).

**Estimated LOC.** ~60 LOC in orchestrator + ~20 LOC in server + ~40 LOC tests. Net ≈ 100 LOC.

---

### 3.7 — Migration plan / feature flag

**Goal.** Path B ships dark behind a flag for all of v0.3's pre-release window. Default OFF preserves v0.2 behavior bit-for-bit.

**Files touched.**

- `manual_test_console/config_schema.py` (around `:204`, in the ALLOWLIST):
  - Add `"orchestrator.use_streaming_speculative": TierBSchemaEntry(key="orchestrator.use_streaming_speculative", code_location="companion_harness/realtime_orchestrator.py:<NEW>", default=False, min=False, max=True, step=1, value_type=bool, description="Enable continuous-proposer Path B with integrated barge-in. v0.3 experimental.")`. (Note: `value_type=bool` requires `TierBSchemaEntry` to support bool; verify the schema entry shape — fall back to `value_type=int` with 0/1 if needed.)

- `manual_test_console/server.py`:
  - Add `--streaming-speculative` argparse flag (default False); when set, calls `config_store.patch("orchestrator.use_streaming_speculative", True)` before `build_live_pipeline` so the orchestrator picks it up from ConfigStore.

- `manual_test_console/live_pipeline.py`:
  - Read `orchestrator.use_streaming_speculative` from ConfigStore at orchestrator-construction time; pass to `StreamingRealtimeOrchestrator(..., use_streaming_speculative=<bool>, native_duplex_eou_source=<MiniCPM source when ON, else _Null>)`.

- `companion_harness/realtime_orchestrator.py`:
  - All Path B code branches are guarded by `if self._use_streaming_speculative:`; legacy Path A code remains untouched.

**Acceptance tests.**
- `tests/test_streaming_speculative_flag.py::test_flag_off_preserves_v02_turn_batched_behavior` — instantiate orchestrator with flag OFF; run a recorded fixture; assert event stream is byte-identical to the v0.2 reference (which is captured at the same fixture in `tests/fixtures/v02_reference_events.jsonl` — to be generated at HEAD `0b49d54`).
- `tests/test_streaming_speculative_flag.py::test_flag_on_uses_streaming_speculative_path` — instantiate with flag ON; assert at least one `proposer_token_buffered` event AND at least one `commit_or_discard` event appears in the captured stream.

**Test matrix expansion.** Add a pytest fixture parameterization to the core orchestrator tests (`tests/test_realtime_orchestrator_streaming.py` and friends) that runs each invariant-contract test under both flag values. Invariants 1, 4, 5, 8, 10 must pass under both.

**Dependencies.** All other sub-plans gate on this for their `use_streaming_speculative=True` test paths.

**Estimated LOC.** ~30 LOC config_schema + ~20 LOC server + ~10 LOC live_pipeline + ~80 LOC matrix tests. Net ≈ 80 LOC of production code, plus matrix-test infra.

---

## §4 — Acceptance criteria (numeric gates)

| Gate | Current (turn-batched, v0.2) | Path B target | Measurement |
|---|---|---|---|
| EOU → first TTS chunk latency, p50 | Unmeasured numerically; bound below by proposer cold-start (200-800 ms) + Kokoro first-chunk (~1-3 s) | **< 800 ms** | New test: `tests/test_path_b_latency_b200.py::test_eou_to_first_tts_chunk_p50` (gpu mark); 20 runs against fixed fixture utterance, report p50. |
| EOU → first TTS chunk latency, p95 | Unmeasured numerically | **< 1500 ms** | Same test, p95. |
| Barge-in window (`is_playing && not is_synthesizing`) | ~5 ms (broken — collapses for short Kokoro utterances) | **> 200 ms** | Existing barge-in test `tests/test_barge_in_*.py` re-run against sub-chunked Kokoro. |
| Barge-in cancellation latency from user-speech onset | (not fired in practice) | **< 500 ms** to `assistant_audio_stop_completed` | New test in 3.4: `test_barge_in_cancels_active_tts_under_500ms`. |
| `synthesis_skipped_no_proposal` rate under load | Non-zero under contention (F0c) | **0 (architecturally impossible — no grace window)** | Manual repro via `tests/manual/repro_f0a_f0b.py auto`; assert zero events. |
| Tier-B replay match rate | 1.0 (existing gate) | **1.0** | `scripts/v0_2_replay_report.py` against a Path B session capture. |
| Invariants 1, 4, 5, 8, 10 contract tests | All pass | **All pass under both flag values** | Matrix-parameterized invariant tests (3.7). |
| Memory growth across 1-hour session | Unmeasured | **< 200 MB RSS delta** (foreground process) | New long-session test in 3.5: `test_long_session_no_unbounded_memory_growth` (gpu mark). |
| `log_drop_or_degrade` rate under 2-utterance synthetic load | ~11-13/run after #305+#321 | **No regression** (≤ same rate) | `repro_f0a_f0b.py auto`, compare drops/run. |

---

## §5 — Risks

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | KV cache resets mid-response, corrupting buffered speculative tokens. | High | §3.5 — coordinate reset to fire only at `is_listen=True` boundaries with no commit in flight; emit `model_context_window_reset` for audit. |
| R2 | Compute waste from discarded speculations under high-discard-rate workloads (e.g., user changes topic frequently, model speculates wrong direction). | Medium | Accepted as the cost of latency. Quantify via `--audit-speculations` flag in forensic sessions; expose `discarded_token_count` in the `commit_or_discard` audit so operators see the cost. |
| R3 | `_last_is_listen` reliability vs VAD-ensemble accuracy. MiniCPM's EOU may be louder/quieter than VAD+SmartTurn under adverse audio. | Medium | Keep VAD as a parallel safety-net EOU producer (`MiniCPMNativeDuplexEouSource` is primary; VAD signal still drives `signal_producer_fallback` when native goes silent). Open question §6.5: what minimum agreement rate before promoting native_duplex to fully replace VAD. |
| R4 | Replay determinism for discarded tokens — naive recording approaches risk inflating the audit log and tripping invariant 10. | Medium | §3.6 — record-decisions-only by default; speculative-token text behind opt-in `--audit-speculations` flag. Invariant 5 is satisfied because the policy decision is deterministic from `PolicyInputs`, and `PolicyInputs` does not depend on discarded proposer tokens (proposer feeds T4, not T2). |
| R5 | Barge-in cancellation races with T4's commit. If barge-in fires AFTER T4 snapshots the ring but BEFORE Kokoro starts emitting chunks, we have a committed snapshot but no in-flight `play_task` to cancel; the snapshot would still be synthesized. | High | Explicit ordering: T4 sets `_audio_output.set_generation_task(play_task)` BEFORE `await play_task`. Barge-in checks `generation_task is None or generation_task.done()` (`:1302-1303`) and emits `barge_in_trigger_no_op` if so. Add a tighter guard: in the snapshot-to-Kokoro window, barge-in queues a deferred cancel that fires on the next `set_generation_task` call. **Action item for coder:** verify there is no >5 ms gap between snapshot at `:1119` and `set_generation_task` at `:1132`; if there is, narrow it. |
| R6 | Migration breakage — flag-day cut over may break v0.2 tests when the orchestrator's constructor signature changes. | Medium | §3.7 — feature flag is a new constructor parameter with default `False`; all v0.2 callers see no change. Matrix test runs every invariant contract under both flag values. |
| R7 | MiniCPM-o per-turn context-folding limitation — `infer_stream` calls `prepare(prefix_system_prompt=combined)` once at the start (`foreground_model_minicpm.py:252`); per-turn `set_context` retrievals cannot be folded mid-session. | Medium-High | Documented in §3.3. Resolution for v0.3: ship with session-level context (`retrieved_items` from the most recent EOU are session-wide, not per-turn). Full per-turn folding is a follow-up ticket against the MiniCPM-o wrapper. |

---

## §6 — Open questions for reviewer

These need explicit go/no-go before §3.3 dispatches. plan-critic should not VERDICT: ready until each has a recorded answer.

1. **Default for `--audit-speculations`.** OFF (recommended) keeps blob volume small but loses byte-exact reconstruction of discarded tokens. ON gives perfect forensics at the cost of ~KB/s blob growth. Recommendation: OFF; operator opts in for forensic captures. **Agree?**

2. **Barge-in cut style.** HARD cut (drop all queued chunks immediately at `request_stop`) vs GRACEFUL fade (let the current sub-chunk finish, then stop). HARD is the cleanest contract and what 3.4's test asserts. GRACEFUL adds 100-200 ms of overlap with the user's new speech, which is conversationally awkward. **Recommendation: HARD. Agree?**

3. **KV cache reset trigger.** Three options: (a) time-based (every N seconds); (b) token-count-based (current MiniCPM-o internal at 1500); (c) MiniCPM's own internal signal (we ride along with the existing reset logic but suppress the mid-response variant). §3.5 picks (c) as it's the minimum diff against MiniCPM-o's actual behavior. **Agree?**

4. **POLICY_VERSION bump.** My read: NO. The speak_policy module is byte-unchanged in this plan; only the proposer's lifecycle changes. The Tier-B replay gate verifies `same_action_class + same_timing_bucket + same_interaction_intent + same_safety_class` (invariant 6) which is computed from `policy_decision` events — those events keep identical schema and identical `decide()` logic. Counter-argument FOR a bump: the *meaning* of "the proposer almost said X but was discarded" is novel to v0.3; a replay-er from v0.2 looking at a v0.3 capture would see new `commit_or_discard` events it doesn't know about. **My read: NO bump. Argue the other side and confirm.**

5. **Promotion threshold for `is_listen` as primary EOU.** Today `MiniCPMNativeDuplexEouSource` is wired as primary but `_NullNativeDuplexEouSource` is the default injection (`realtime_orchestrator.py:317-319`). Path B requires the real source. What agreement rate with VAD+SmartTurn (over a captured 100-turn session) is acceptable before we ship Path B as the default? My proposal: ≥95% agreement on EOU timing within ±300 ms. Below that, leave the flag OFF in production. **Agree on 95% / 300 ms?**

---

## §7 — Cross-references

- `docs/basic-stack-design-2026-05-16.md` — basic stack this plan extends; §4 risk table cites the same Concerns 6 / F0c / F1 design defect this plan addresses.
- `docs/manual-test-findings-2026-05-16.md` — F0a (race), F0c (`synthesis_skipped_no_proposal`), F1 (design defect), and the barge-in test reference `#aebe6` (Concern 6 in basic-stack-design).
- `CLAUDE.md` invariants: this plan preserves **#1** (every new event has `caused_by`), **#2** (proposer never speaks directly; T4 + policy still gate), **#4** (no proactive speech without policy approval), **#5** (policy replay bit-identical; new `commit_or_discard` events are deterministic from inputs), **#8** (silence still wins ties), **#10** (EventLogger non-blocking; speculative-token events use the same async pattern as `_emit_invocation`).
- Issue #320 (wake-word-on-audio addressing) — orthogonal but related: both address the "model decides to speak from audio without a text gate" gap. Path B does not subsume #320; F1's voice-mode addressing redesign is a separate v0.3 milestone slot.
- PR #306 (F0a/F0b/F0c fixes — `_decision_in_flight` reset to `finally`, inter-batch frame drain, grace 200→600 ms) — Path B inherits the finally-reset pattern; the inter-batch drain becomes dead code under flag ON.
- PR #308 (schema bump `proposal_batch_window_ms` default 80→600 / max 200→1500) — Path B makes this knob irrelevant when flag ON; the schema stays for Path A backward compat.
- PR #317 (G1 — `MiniCPMStreamingModel.set_session` wires logger so `native_duplex_invocation` events log under session) — this plan depends on it: every new `proposer_token_buffered` / `model_context_window_reset` event goes through the same session-bound logger.
- PR #321 (R2-3 — DisplayBroker queue 256→1024, default-off high-volume filter) — Path B adds two new high-volume event types (`proposer_token_buffered` could fire 1 Hz under continuous consumption); add them to the default-off filter list as part of 3.3's frontend touch.
- PR #322 (logprob-based addressing classifier) — unchanged by this plan; addressing path runs identically under both flags.
- PR #323 (auto-wait-for-tts driver verify mode) — verify F0a is still architecturally impossible under Path B (no batched proposer ⇒ no stale-frame race).

---

## §8 — Estimated effort and suggested PR sequence

| Sub-plan | Approx LOC | Risk | Independent? |
|---|---|---|---|
| 3.1 Kokoro sub-chunking | ~50 + ~40 test | Low | Yes — ships immediately. |
| 3.2 `_dropped_before_enqueue` root-cause | ~80 + ~60 test | Low (after diagnosis) | Yes — parallel with 3.1. |
| 3.7 Flag skeleton | ~80 + ~80 matrix test | Low | Lands after 3.1 (needs an existing PR to test against). |
| 3.3 Path B core | ~600 + ~200 test | High — touches T3/T4/EOU routing | Lands under flag, depends on 3.7. |
| 3.4 Integrated barge-in | ~250 + ~150 test | Medium-High | Depends on 3.1 (sub-chunking) and 3.3 (ring). |
| 3.5 KV cache windowed reset | ~150 + ~80 test | Medium | Depends on 3.3 (T4 calls set_committing). |
| 3.6 Audit story | ~100 + ~40 test | Low | Depends on 3.3 (the events being audited). |

**Suggested PR sequence (linearized).**

1. **PR 1:** 3.1 (sub-chunking, isolated). Ships immediately.
2. **PR 2 (parallel with PR 1):** 3.2 (drops root-cause). Ships independently.
3. **PR 3:** 3.7 (flag skeleton). Adds the config key + CLI flag + matrix test; orchestrator constructor learns the flag but does nothing with it yet. Net behavior: zero change.
4. **PR 4:** 3.3 (Path B core, under flag). Big — the gated continuous-consumption + ring + commit/discard. Flag OFF preserves v0.2 bit-for-bit; flag ON activates the new path. Includes the `MiniCPMNativeDuplexEouSource` wiring change.
5. **PR 5:** 3.4 (barge-in). Builds on 3.1 + 3.3.
6. **PR 6:** 3.5 (KV reset coordination). Builds on 3.3.
7. **PR 7:** 3.6 (audit instrumentation + `--audit-speculations`). Builds on 3.3.
8. **PR 8 (flag-day cut-over):** flip `orchestrator.use_streaming_speculative` default from `False` → `True` in `config_schema.py`. This is the only PR that changes user-visible behavior in main; gate on operator sign-off after Open Question §6.5 is answered.

Total approximate effort: ~1300 production LOC, ~650 test LOC, ~7 weeks across dev + review + manual-test cycles assuming one developer with operator gate at each PR boundary.
