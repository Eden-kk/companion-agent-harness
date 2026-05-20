# Sub-plan: PR5b — live `--continuous` wiring (run ContinuousOrchestrator in the console)

**Status:** READY (round 2, converged).
**Parent:** `plan-turn-free-continuous-execution.md` PR5b. **Stacks on:** `port/pr5a-listen-prob-tuning` (#366), which sits on `origin/main`'s merged turn-free stack (#357/#360/#361/#362/#363) + the ports #364/#365.
**One outcome:** a `--continuous` flag makes the manual-test console run the **`ContinuousOrchestrator`** live pipeline (fed by the same WebSocket mic → `audio_in` path, emitting per-chunk audit events, starting/stopping speech via `AudioOutputController`) instead of the turn-based `StreamingRealtimeOrchestrator`. **Default OFF** — the turn-based path is byte-identical when the flag is unset.

## Scope boundary (what PR5b ships now vs. operator-gated)

**This PR (codeable, CPU-testable, reversible):** the `--continuous` wiring, default OFF: flag → app-context → handler branch → a new `ContinuousLivePipeline` that runs `ContinuousOrchestrator.run()` as one task. Plus a CPU integration smoke test (fake model) proving the continuous pipeline wires and runs end-to-end through the real `audio_in` plumbing.

**Deferred / operator-gated (NOT this PR):**
- **Default-on flip** (`--continuous` → default True) = a one-line follow-up needing **human sign-off** after a clean manual-test cycle (execution plan §PR5b).
- **b200 measured-and-recorded gates:** barge-in **p95** at `chunk_ms=200` (+`torch.compile`) within the Stage-1 budget; **incorporation ≥80%@N≥10**; **false_proactive/hour** within target; the manual mic cycle. No end-to-end continuous path existed before this PR, so these are measured *after* it lands, recorded in the PR/issue, and gate the flip — not this merge.
- **Live `backchannel_source`** (adapter wrapping the real `BackchannelClassifier` → `latest_score()`) and **live `thought_source`** (real background reasoner). PR5b wires the **null** sources (the seams from #364/#365); the live adapters are follow-ups. The 200ms-chunk fallback-to-250 decision is made at flip time with the real p95.

## Current wiring (verified against the worktree — coder must re-read these)

- `manual_test_console/server.py`: `_load_minicpm_streaming_model(...)` (~1888-1901) accepts `chunk_ms` **but NOT `listen_prob_scale`** (that lives only on `MiniCPMStreamingModel.__init__`); CLI flag `--enable-live-pipeline` (~2001-2006); `factory = _load_minicpm_streaming_model` no-arg form (~2299); `/ws/ingest` handler (~722-747) calls `build_live_pipeline(foreground_duplex_model=…)` then `pipeline.start()`, and pushes audio via `pipeline.push_audio(payload_bytes, evt.event_id, ts_mono)` (~781, **3 positional args**). Per-request config is read via `request.app[KEY_*]` (e.g. `KEY_LIVE_PIPELINE_ENABLED`), set in `build_app`.
- `manual_test_console/live_pipeline.py`: `build_live_pipeline()` (~483-776) creates `audio_in = asyncio.Queue(maxsize=64)` (~543), wraps the model in a **`ForegroundModel` adapter (~611)**, constructs `AudioOutputController(session_id, logger, sink)` (~620-624) and `StreamingRealtimeOrchestrator` (~712-743). **`LivePipeline` (~413-443) is a CONCRETE dataclass** with field `orchestrator: StreamingRealtimeOrchestrator`; `start()` (~436) delegates to `orchestrator.start()` (the 5-task graph); `stop()` (~) cancels those tasks; `push_audio(frame_bytes, raw_audio_event_id, ts_mono_ms=0)` (~445-466, non-blocking, drop-oldest). → **`LivePipeline` cannot host `ContinuousOrchestrator`** (which has no `start()`, only `run()`); a sibling wrapper is required (see Design 4).
- `companion_harness/continuous_orchestrator.py`: `ContinuousOrchestrator.__init__(*, session_id, logger, audio_in, foreground_model, audio_output, backchannel_source=…, thought_source=…, privacy_mode, social_mode, budget_full_response_remaining)`; `async def run(self)` consumes `foreground_model.stream_chunks(audio_in)` until the `(b"", "")` sentinel. **`stream_chunks` lives on the raw `MiniCPMStreamingModel`, NOT the `ForegroundModel` wrapper** — pass the raw duplex model directly.
- `AudioOutputController` (`audio_output_controller.py`): exposes exactly `is_playing` (prop), `start_generation(caused_by)`, `request_stop(caused_by)` — **matches `ContinuousOrchestrator._AudioOutputProtocol`** (reuse as-is).

## Design (all critic blockers/concerns resolved)

1. **Flag + app-context.** Add `--continuous` to server.py argparse (default `False`); thread it into `build_app` (new param) and store as `app[KEY_CONTINUOUS]`, mirroring `KEY_LIVE_PIPELINE_ENABLED`. The `/ws/ingest` handler reads `request.app[KEY_CONTINUOUS]` to branch. (`--continuous` implies the live pipeline.)
2. **Model load.** Extend `_load_minicpm_streaming_model` to accept `listen_prob_scale: float | None = None` and forward it to `MiniCPMStreamingModel(...)` (mirrors the existing `chunk_ms` forward). When `--continuous`, build the singleton with `chunk_ms=200, listen_prob_scale=1.0` (validated tuning from #363/#366). Default path unchanged (`chunk_ms=1000`, `listen_prob_scale=None`).
3. **Factory.** Add `build_continuous_pipeline(*, session_id, logger, foreground_duplex_model, audio_out_broker=…, …)` in `live_pipeline.py`, minimal: create the same `audio_in = asyncio.Queue(maxsize=64)`; construct `AudioOutputController(session_id, logger, sink)` (same as turn-based); construct `ContinuousOrchestrator(session_id=…, logger=…, audio_in=audio_in, foreground_model=foreground_duplex_model, audio_output=…)` with **null** backchannel/thought sources (defaults). **Pass the raw `foreground_duplex_model` directly — do NOT wrap in `ForegroundModel`** (stream_chunks lives on the raw model). **Skip** VAD/SmartTurn/Backchannel/ASR/TTS-adapter/ingest_session/vision/addressing wiring.
4. **`ContinuousLivePipeline` wrapper (NEW — mandatory, not a reuse of `LivePipeline`).** A minimal dataclass holding `orchestrator: ContinuousOrchestrator`, `audio_in`, and a task handle, exposing the SAME interface the handler uses:
   - `push_audio(frame_bytes, raw_audio_event_id, ts_mono_ms=0)` → non-blocking put on `audio_in`, **drop-oldest on overflow** (mirror `LivePipeline.push_audio` exactly, incl. the 3rd `ts_mono_ms` arg — accepted and ignored, since `ContinuousOrchestrator` has no diarization).
   - `start()` → `self._task = asyncio.create_task(self._orchestrator.run())`.
   - `stop()` → push the `(b"", "")` sentinel onto `audio_in`, then `await asyncio.wait_for(self._task, timeout=…)`; on `TimeoutError` (or always, belt-and-suspenders) `self._task.cancel()` + await-suppressing-`CancelledError`. **Both** the real model (whose `stream_chunks` honors the sentinel) and a scripted test fake terminate cleanly; no pending-task warning.
   `build_continuous_pipeline` returns a `ContinuousLivePipeline`.
5. **Handler branch.** In `/ws/ingest`, `if request.app[KEY_CONTINUOUS]: pipeline = build_continuous_pipeline(...)` else `build_live_pipeline(...)`. Downstream (`push_audio`/`start`/`stop`) is interface-identical, so the rest of the handler is unchanged.

## File-by-file

| File | Change |
|---|---|
| `manual_test_console/server.py` | `--continuous` arg (default False); thread through `build_app` → `app[KEY_CONTINUOUS]` (+ define `KEY_CONTINUOUS`); when set, model factory calls `_load_minicpm_streaming_model(chunk_ms=200, listen_prob_scale=1.0)` (signature extended per Design 2); `/ws/ingest` branches to `build_continuous_pipeline`. |
| `manual_test_console/server.py` (`_load_minicpm_streaming_model`) | add `listen_prob_scale: float | None = None` param, forward to `MiniCPMStreamingModel(...)`. |
| `manual_test_console/live_pipeline.py` | add `build_continuous_pipeline(...)` (ContinuousOrchestrator + audio_in + AudioOutputController, null BC/thought, raw model, no detectors/TTS) + a `ContinuousLivePipeline` dataclass (`push_audio`/`start`/`stop` per Design 4). |
| `tests/test_continuous_live_pipeline.py` (new) | CPU smoke test (§gate). |

## Success criterion (sole programmatic gate)

`tests/test_continuous_live_pipeline.py` — CPU, no GPU/weights:
- **End-to-end smoke:** call `build_continuous_pipeline` with a **fake foreground model whose `stream_chunks` mirrors the real adapter** — i.e. it **consumes `(pcm, evt)` from `audio_in`, accumulates `_CHUNK_SAMPLES`, yields one record per full chunk, and returns on the `(b"", "")` sentinel** (copy the proven fake from `tests/test_continuous_orchestrator_feeder.py`) — plus a noop/recording audio sink + a real `EventLogger`. Assert: the returned object is a `ContinuousLivePipeline` whose `orchestrator` is a `ContinuousOrchestrator`; `start()`, then `push_audio(chunk, evt_id, ts)` for N full chunks, then `stop()` ⇒ exactly **N `continuous_chunk_processed` events** with closed `caused_by` (end-to-end through the real `audio_in` queue + lifecycle); `stop()` returns without a pending-task warning (both sentinel-honored and cancel-safe).
- **Regression:** `build_live_pipeline` (the `--continuous`-off path) still builds a `StreamingRealtimeOrchestrator` — assert the selection logic, with a fake model, returns a `LivePipeline` (don't break the turn-based path). (If constructing `build_live_pipeline` on CPU is too heavy, instead assert the handler's branch picks the right builder via the `KEY_CONTINUOUS` flag with both builders monkeypatched to sentinels.)

The b200 barge-in p95 / incorporation / false_proactive / manual mic cycle are **measured-and-recorded** post-merge (operator-gated), not asserted here.

## Invariants

- **#1/#10:** per-chunk events via the existing `ContinuousOrchestrator` path (already audited); `push_audio` stays non-blocking (drop-oldest), realtime path never waits on log durability.
- **No regression:** `--continuous` default OFF ⇒ turn-based path unchanged; default model load args unchanged; `_load_minicpm_streaming_model`'s new param defaults to `None` (no behavior change).
- **Adapter-first:** the console wires adapters; no model SDK in the orchestrator.

## Risks / open questions

| Item | Handling |
|---|---|
| `LivePipeline` can't host a single-task orchestrator (concrete dataclass typed to `StreamingRealtimeOrchestrator`) | **Resolved:** add a NEW `ContinuousLivePipeline` (Design 4), do not reuse/extend `LivePipeline`. |
| `stop()` hangs if the model's `stream_chunks` ignores `audio_in` | **Resolved:** `stop()` pushes sentinel AND awaits-with-timeout-then-cancels; the test fake consumes `audio_in` + honors the sentinel (happy path), cancel is the safety net. |
| `_load_minicpm_streaming_model` lacks `listen_prob_scale` | **Resolved:** extend its signature (Design 2). |
| `push_audio` 3-arg signature | **Resolved:** `ContinuousLivePipeline.push_audio(frame_bytes, raw_audio_event_id, ts_mono_ms=0)` (ts ignored). |
| handler config access pattern | **Resolved:** `KEY_CONTINUOUS` via `build_app` → `app[...]` (Design 1). |
| Real model not exercised in CI | smoke test injects a fake model; real chunk_ms=200 behavior is the operator b200 cycle. |

## Out of scope

Default-on flip (operator sign-off); b200 p95 / incorporation / false_proactive probes + manual mic cycle; live `backchannel_source` + `thought_source` adapters; PR6 (delete turn machinery).
