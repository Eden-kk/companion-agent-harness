# Sub-plan: PR1 — Continuous feeder + sliding window

**Status:** DRAFT (round 0) — for plan-critic review.
**Parent:** [`plan-turn-free-continuous-execution.md`](plan-turn-free-continuous-execution.md) PR1.
**Design:** [`design-turn-free-continuous-companion.md`](design-turn-free-continuous-companion.md) §4–§6.
**One outcome:** a new `continuous_orchestrator.py` that consumes audio chunks continuously (no per-turn drain), runs the duplex model per chunk with `sliding_window_mode="context"`, and emits per-chunk events with closed `caused_by[]`. Policy is a placeholder (always-silence) replaced in PR2.

This is the implementation-level HOW for PR1. It does **not** wire the per-chunk policy gate (PR2), the model-native barge-in (PR3), or background-think (PR4).

---

## 1. What exists to reuse (grounding)

- `manual_test_console/live_pipeline.py::build_live_pipeline` (line 487) constructs all adapters (MiniCPM-o foreground, VAD/SmartTurn/backchannel, ASR, addressing, memory, TTS, AudioOutputController, EventLogger). PR1 reuses this construction — it does **not** rebuild adapters.
- `companion_harness/realtime_orchestrator.py` (2610 lines, turn-based): the reference for adapter wiring, the `audio_in: asyncio.Queue[tuple[bytes,str]]` contract (line 255), `_audio_tee_task` (line 536), and EventLogger usage. PR1 does **not** modify it.
- `companion_harness/foreground_model_minicpm.py::MiniCPMStreamingModel`: `_duplex` (the `MiniCPMODuplex`), `streaming_prefill(audio_waveform=)`, `streaming_generate() -> {is_listen, text, ...}`, `_emit_invocation`. PR1 drives this per chunk.
- `companion_harness/evals/scenarios/{synthetic_clock,audio_feeder,fixture}.py`: `SyntheticClock`, `DirectAudioInputFeeder`, `FixtureScenarioDriver` — the deterministic feed path for the success test.

## 2. The continuous loop (the core change)

Unlike the turn-based orchestrator (T1→T2→T3→T4 with `_decision_in_flight`, `_batch_open/close`, per-response drain), the continuous orchestrator runs **one loop**:

```
ContinuousOrchestrator.run():
    duplex.prepare(prefix_system_prompt=...)          # once, at session start
    while not stopped:
        frame_bytes, chunk_evt_id = await audio_in.get()      # same queue contract as today
        pcm = pcm16_to_float(frame_bytes)
        accumulate into a 1s (chunk_ms) buffer
        when buffer full:
            result = await run_in_executor(duplex.streaming_prefill + streaming_generate)
            is_listen = result["is_listen"]; text = result["text"]
            emit continuous_chunk_processed event  (caused_by=[chunk_evt_id]; payload_inline:
                                                    {is_listen, audio_kv_len, n_chars})
            decision = self._policy_hook(...)         # PR1: ALWAYS returns silence
            # PR1 stops here. (PR2 wires decide_chunk; PR3 wires barge-in; PR4 wires think-injection.)
```

Key properties:
- **No EOU batching, no drain task, no `_decision_in_flight`.** The loop is flat.
- **Same `audio_in` queue contract** as the turn-based orchestrator, so the existing ingest path (`/ws/ingest`) and the eval `DirectAudioInputFeeder` both feed it unchanged.
- **`run_in_executor`** for the GPU call (single-worker executor, as `MiniCPMStreamingModel` already uses), so the event loop keeps draining `audio_in`.
- **Buffering:** accumulate incoming ~30 ms transport frames into one `chunk_ms` (default 1000) chunk before `streaming_prefill`, matching today's `infer_stream`.

## 3. Placeholder policy hook

PR1 ships `self._policy_hook` returning `SpeakDecision(action_type="silence", primary_reason_code=...)` unconditionally. It is the explicit seam PR2 replaces with `decide_chunk`. **PR1 MUST NOT call `speak_policy.decide`** (per-turn schema, incompatible). No TTS is dispatched in PR1 (silence only) — so PR1 exercises the feed + per-chunk model loop + event emission, nothing more.

## 4. Sliding window

Set `sliding_window_mode="context"` on the duplex (via `as_duplex(..., sliding_window_mode="context")` or the duplex-params kwarg). This is the only change to `foreground_model_minicpm.py`, gated so the turn-based path keeps its current default (`"off"`). Continuous feeder + sliding window ship together (design §5).

## 5. Audio-KV-reset handling

<!-- PROBE-RESULTS-START -->
**Result: GO** (`scripts/probe_audio_kv_reset.py`, 2026-05-19, b200/MiniCPM-o 4.5). With the model monologuing (speak-biased so it's actively generating), the audio-KV reset fired at chunk 30 (1450→50 tokens, the ~1500 cap) and the model **continued coherently across the boundary**: before — "…the bustling streets of Tokyo"; after — "…geishas moved gracefully through hidden alleyways" (same scene, narrative continuity held; both coherent, unique-ratio 0.9–1.0). The story lives in the LLM backbone (`llm_past_key_values`, not reset); only the audio-encoder cache reset, which does not break generation.

**PR1 handling:** no special mitigation. Emit an `audio_kv_reset` audit event when detected (observability, invariant #1) and continue.

**Caveat / follow-up (not a PR1 blocker):** this tests coherence of the model's *own ongoing generation* across the reset. It does not test whether the model retains memory of *user audio* spoken before the reset (that context lives partly in the audio cache that gets wiped). A fact-retention-across-reset probe (user states a fact pre-reset; ask post-reset) is a reasonable follow-up if long-session user-audio recall matters — defer to PR5/PR6 long-session validation.
<!-- PROBE-RESULTS-END -->

## 6. File-by-file

| File | Change |
|---|---|
| `companion_harness/continuous_orchestrator.py` (new) | `ContinuousOrchestrator` class: ctor takes the same adapters as `RealtimeOrchestrator` + `audio_in` queue + EventLogger; `run()` implements §2; `_policy_hook` placeholder (§3); emits `continuous_chunk_processed` + `audio_kv_reset` events. |
| `companion_harness/foreground_model_minicpm.py` | add `sliding_window_mode` duplex-param (default unchanged for turn-based; `"context"` when continuous). Surgical; no behavior change to existing path. |
| `manual_test_console/live_pipeline.py` | `--continuous` kwarg (default False) in `build_live_pipeline`; when True, build `ContinuousOrchestrator` instead of the turn-based one, sharing the same adapters. |
| `manual_test_console/server.py` | thread `--continuous` CLI flag through to `build_live_pipeline`. |
| `tests/test_continuous_orchestrator_feeder.py` (new) | the success-criterion test (§7). |

## 7. Success criterion (sole programmatic gate)

`tests/test_continuous_orchestrator_feeder.py::test_continuous_orchestrator_emits_per_chunk_events` — **distinct file** from the existing `tests/test_continuous_feeder.py` (legacy `StreamingRealtimeOrchestrator`; do not touch). Under `SyntheticClock` + `DirectAudioInputFeeder`, a fixture WAV runs through `ContinuousOrchestrator` end-to-end and:
- emits ≥1 `continuous_chunk_processed` event per consumed chunk,
- every emitted event has closed `caused_by[]` (no orphans; invariant #1),
- no TTS/`assistant_audio_*` events fire (PR1 is silence-only),
- the turn-based suite is regression-green.

Plus: the audio-KV-reset probe (§5) recorded GO/NO-GO in this sub-plan before merge (separate precondition, not part of the test gate).

## 8. Invariants

- **#1 no unlogged behavior:** every chunk → a logged `continuous_chunk_processed` with `caused_by`.
- **#5 determinism:** PR1 has no policy decisions yet (placeholder silence), so nothing to replay; PR2 introduces the deterministic gate. PR1's events must still be replay-safe (enum/numeric payload only).
- **#10 EventLogger async:** the loop calls non-blocking `EventLogger.log`; no awaiting log durability on the audio path.

## 9. Risks / open questions

| Item | Handling |
|---|---|
| Reusing `build_live_pipeline` may pull turn-based assumptions | Build only the *adapters* via the shared path; the orchestrator class is new. Confirm `build_live_pipeline` can return adapters without instantiating the turn-based orchestrator (may need a small refactor to split adapter-construction from orchestrator-construction). |
| `streaming_generate` executor contention with the live server | PR1 runs under test (SyntheticClock) without the live server; the executor is per-model-instance and serialized. |
| Per-chunk event volume | `continuous_chunk_processed` at ~1/chunk is fine; do not emit per-transport-frame. Sample high-rate sub-events as the turn-based path does. |
| `chunk_ms` default | PR1 keeps `chunk_ms=1000`; PR5a tunes toward 200 ms. |

## 10. Out of scope (later PRs)

Per-chunk policy gate (PR2), model-native barge-in / gate-relax (PR3), background-think injection (PR4), tuning (PR5a). PR1 is feed + per-chunk model loop + events + sliding window, nothing more.
