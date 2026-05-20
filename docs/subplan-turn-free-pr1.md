# Sub-plan: PR1 — Continuous feeder + sliding window

**Status:** DRAFT (round 0) — for plan-critic review.
**Parent:** [`plan-turn-free-continuous-execution.md`](plan-turn-free-continuous-execution.md) PR1.
**Design:** [`design-turn-free-continuous-companion.md`](design-turn-free-continuous-companion.md) §4–§6.
**One outcome:** a new `continuous_orchestrator.py` that consumes audio chunks continuously (no per-turn drain), runs the duplex model per chunk with `sliding_window_mode="context"`, and emits per-chunk events with closed `caused_by[]`. Policy is a placeholder (always-silence) replaced in PR2.

This is the implementation-level HOW for PR1. It does **not** wire the per-chunk policy gate (PR2), the model-native barge-in (PR3), or background-think (PR4).

---

## 1. What exists to reuse (grounding)

- `companion_harness/realtime_orchestrator.py` (2610 lines, turn-based): the reference for adapter wiring, the `audio_in: asyncio.Queue[tuple[bytes,str]]` contract (line 255), `_audio_tee_task` (line 536), and EventLogger usage. **`ContinuousOrchestrator.__init__` takes its adapters as constructor args, mirroring this ctor pattern.** PR1 does **not** modify it.
- `companion_harness/foreground_model_minicpm.py::MiniCPMStreamingModel`: `_duplex` (the `MiniCPMODuplex`), `streaming_prefill(audio_waveform=)`, `streaming_generate() -> {is_listen, text, ...}`, `_emit_invocation`. PR1 drives this per chunk.
- `companion_harness/evals/scenarios/{synthetic_clock,audio_feeder,fixture}.py`: `SyntheticClock`, `DirectAudioInputFeeder`, `FixtureScenarioDriver` — the deterministic feed path for the success test.
- **`build_live_pipeline` is NOT reused in PR1.** It always instantiates the turn-based `StreamingRealtimeOrchestrator` (`live_pipeline.py:719–751`) and cannot hand back bare adapters. The PR1 test constructs adapters directly with stubs (the pattern existing tests already use, e.g. `tests/test_continuous_feeder.py`). Live wiring (`--continuous` in `build_live_pipeline` + `server.py`) is **deferred to a later PR** (when speech actually routes somewhere).

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

The duplex parameter is `sliding_window_mode` (defined in `modeling_minicpmo.py` `_default_duplex_params`; default `"off"`; valid modes `"off"` / `"context"` / `"basic"`). Plumb it via a new `sliding_window_mode: str = "off"` argument on `MiniCPMStreamingModel.__init__`, passed through to `base.as_duplex(generate_audio=False, sliding_window_mode=sliding_window_mode)`. The continuous path passes `"context"`; the turn-based path keeps the `"off"` default (surgical; no behavior change to existing path). This is the only change to `foreground_model_minicpm.py`.

The real-model `sliding_window_mode="context"` behavior is verified in a manual/b200 run, **not** the CPU unit test (which uses a `FakeDuplex` stub — see §7). Continuous feeder + sliding window ship together (design §5).

## 5. Audio-KV-reset handling

<!-- PROBE-RESULTS-START -->
**Result: GO** (`scripts/probe_audio_kv_reset.py`, 2026-05-19, b200/MiniCPM-o 4.5). With the model monologuing (speak-biased so it's actively generating), the audio-KV reset fired at chunk 30 (1450→50 tokens, the ~1500 cap) and the model **continued coherently across the boundary**: before — "…the bustling streets of Tokyo"; after — "…geishas moved gracefully through hidden alleyways" (same scene, narrative continuity held; both coherent, unique-ratio 0.9–1.0). The story lives in the LLM backbone (`llm_past_key_values`, not reset); only the audio-encoder cache reset, which does not break generation.

**PR1 handling:** no special mitigation. Emit an `audio_kv_reset` audit event when detected (observability, invariant #1) and continue. Detection: the orchestrator tracks `duplex.model.audio_past_key_values` length externally each chunk (reusing the `_audio_kv_len` helper from `scripts/probe_audio_kv_reset.py`) and emits `audio_kv_reset` when the length drops versus the prior chunk. This is NOT a `streaming_generate` return field.

**Caveat / follow-up (not a PR1 blocker):** this tests coherence of the model's *own ongoing generation* across the reset. It does not test whether the model retains memory of *user audio* spoken before the reset (that context lives partly in the audio cache that gets wiped). A fact-retention-across-reset probe (user states a fact pre-reset; ask post-reset) is a reasonable follow-up if long-session user-audio recall matters — defer to PR5/PR6 long-session validation.
<!-- PROBE-RESULTS-END -->

## 6. File-by-file

| File | Change |
|---|---|
| `companion_harness/continuous_orchestrator.py` (new) | `ContinuousOrchestrator` class: ctor takes adapters as constructor args (mirroring `RealtimeOrchestrator`) + `audio_in` queue + EventLogger; `run()` implements §2; `_policy_hook` placeholder (§3); emits `continuous_chunk_processed` + `audio_kv_reset` events. |
| `companion_harness/foreground_model_minicpm.py` | add `sliding_window_mode: str = "off"` to `MiniCPMStreamingModel.__init__`, passed through to `base.as_duplex(...)`. Default unchanged for turn-based; `"context"` when continuous. Surgical; no behavior change to existing path. |
| `tests/test_continuous_orchestrator_feeder.py` (new) | the success-criterion test (§7). |

`manual_test_console/live_pipeline.py` and `manual_test_console/server.py` are **not touched in PR1**. Live wiring (`--continuous` CLI flag) is deferred to a later PR (see §1).

## 7. Success criterion (sole programmatic gate)

`tests/test_continuous_orchestrator_feeder.py::test_continuous_orchestrator_emits_per_chunk_events` — **distinct file** from the existing `tests/test_continuous_feeder.py` (legacy `StreamingRealtimeOrchestrator`; do not touch). The test is **CPU-runnable (CI-safe)**: `ContinuousOrchestrator` accepts the foreground model via injection; the test injects a synchronous `FakeDuplex` stub whose `streaming_generate` returns `{"is_listen": True, "text": ""}` and whose `streaming_prefill` is a no-op. No real MiniCPM-o weights are loaded.

Under `SyntheticClock` + `DirectAudioInputFeeder` + `FakeDuplex`, a fixture WAV runs through `ContinuousOrchestrator` end-to-end and:
- emits ≥1 `continuous_chunk_processed` event per consumed chunk,
- every emitted event has closed `caused_by[]` (no orphans; invariant #1),
- no TTS/`assistant_audio_*` events fire (PR1 is silence-only),
- the turn-based suite is regression-green.

The `sliding_window_mode="context"` real-model behavior and `audio_kv_reset` emission are verified/recorded in a manual/b200 run, **not** this CPU gate. Plus: the audio-KV-reset probe (§5) recorded GO/NO-GO in this sub-plan before merge (separate precondition, not part of the test gate).

## 8. Invariants

- **#1 no unlogged behavior:** every chunk → a logged `continuous_chunk_processed` with `caused_by`.
- **#5 determinism:** PR1 has no policy decisions yet (placeholder silence), so nothing to replay; PR2 introduces the deterministic gate. PR1's events must still be replay-safe (enum/numeric payload only).
- **#10 EventLogger async:** the loop calls non-blocking `EventLogger.log`; no awaiting log durability on the audio path.

## 9. Risks / open questions

| Item | Handling |
|---|---|
| `streaming_generate` executor contention with the live server | PR1 runs under test (SyntheticClock) without the live server; the executor is per-model-instance and serialized. |
| Per-chunk event volume | `continuous_chunk_processed` at ~1/chunk is fine; do not emit per-transport-frame. Sample high-rate sub-events as the turn-based path does. |
| `chunk_ms` default | PR1 keeps `chunk_ms=1000`; PR5a tunes toward 200 ms. |
| `LivePipeline.orchestrator` is concretely typed `StreamingRealtimeOrchestrator` | The future live-wiring PR (that adds `--continuous` to `build_live_pipeline`) will need an `Orchestrator` protocol or a widened type annotation. Out of scope for PR1. |

## 10. Out of scope (later PRs)

Per-chunk policy gate (PR2), model-native barge-in / gate-relax (PR3), background-think injection (PR4), tuning (PR5a). PR1 is feed + per-chunk model loop + events + sliding window, nothing more.
