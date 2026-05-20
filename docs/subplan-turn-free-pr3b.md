# Sub-plan: PR3b — Model-native barge-in (gate-relax + stop on yield)

**Status:** DRAFT (round 1, post-critic) — for plan-critic re-review.
**Parent:** `plan-turn-free-continuous-execution.md` PR3 (split). **Stacks on:** `feat/turn-free-pr3a`.
**One outcome:** the model's own mid-utterance `is_listen` yield stops the assistant's speech (model-native barge-in), by relaxing the mid-turn gate **instance-safely** and wiring a stop path in the orchestrator.

## 1. Instance-safe gate-relax

The gate is in the HF model file (`modeling_minicpmo.py:3215`); relax it at runtime by reassigning **only `self._duplex`'s `__class__`** to a per-instance subclass carrying the descriptor — done **inside `stream_chunks`, which is the continuous-only method** (the turn-based path uses `infer_stream`, untouched). No flag is needed (round-1: the only caller always relaxes; a `relax_turn_gate` param was overdesign — dropped).

```python
# in MiniCPMStreamingModel.stream_chunks(), wrapping prepare() AND the loop:
orig_cls = type(self._duplex)
relaxed_cls = type(f"{orig_cls.__name__}_GateRelaxed", (orig_cls,),
                   {"current_turn_ended": _AlwaysEnded()})   # data descriptor: get→True, set→no-op
self._duplex.__class__ = relaxed_cls
try:
    self._duplex.prepare(...)          # MUST be inside the try (else an exception here leaks the subclass)
    ... per-chunk loop ...
finally:
    self._duplex.__class__ = orig_cls  # always restore
```

**Why this is safe (corrected rationale):** the round-0 conflict was specific to a *class-level* `type(duplex).current_turn_ended = ...`, which both `self._duplex` and the native-TTS adapter's duplex would share. The `__class__` swap is **per-object** and structurally cannot reach `MiniCPMNativeTtsAdapter._duplex_tts` (a different instance on a different adapter, `tts_minicpm_native.py:128`). `__class__` reassignment is valid (`MiniCPMODuplex` has no `__slots__`); the data descriptor shadows the instance's `current_turn_ended` for `self._duplex` only; `finally` restores it.

`_AlwaysEnded` is added module-level in `foreground_model_minicpm.py` (the probe's copy at `scripts/probe_turn_gate_barge_in.py:143` is not importable from `companion_harness/` — duplication is unavoidable; note it so review doesn't flag it).

## 2. Barge-in stop routing

`ContinuousOrchestrator._act` gains a stop branch; `run()`'s existing `_act(...)` call site (currently `self._act(decision, caused_by_evt_id)`) **must be updated to pass `is_listen`**:

```python
def _act(self, decision, is_listen, caused_by_evt_id):
    speaking = self._audio_output.is_playing
    if is_listen and speaking:                       # model chose listen while we spoke → barge-in
        self._audio_output.request_stop(caused_by=[caused_by_evt_id])
        self._emit_barge_in(caused_by_evt_id)        # model_native_barge_in event
        return
    if decision.action_type in _SPEAK_ACTIONS and not speaking:
        self._audio_output.start_generation(caused_by=[caused_by_evt_id])
```

The real stop method is **`request_stop(caused_by) -> str`** (non-blocking; `AudioOutputController` has `request_stop`/`cancel_generation`, NOT `stop`). `_AudioOutputProtocol` gains `request_stop(self, *, caused_by: list[str]) -> str`. Silence wins ties (#8): barge-in *stops* speech.

## 3. File-by-file

| File | Change |
|---|---|
| `companion_harness/foreground_model_minicpm.py` | module-level `_AlwaysEnded`; in `stream_chunks`, inner `try/finally` `__class__` swap wrapping `prepare()` + loop (§1). No new param. |
| `companion_harness/continuous_orchestrator.py` | `_act` stop branch (§2) + pass `is_listen` at the `run()` call site; add `request_stop` to `_AudioOutputProtocol`; `_emit_barge_in`. |
| `companion_harness/v0_1g_event_schema.py` | register `model_native_barge_in` (signal/self/safe/signal_default_30d) + EXPECTED_EVENT_TYPES. |
| `tests/test_continuous_pr3b_barge_in.py` (new) | CPU success test (§4). |

## 4. Success criterion (sole programmatic gate)

`tests/test_continuous_pr3b_barge_in.py::test_model_yield_stops_speech` — CPU, stubs only (no torch import). Script the FakeForegroundModel is_listen sequence `[speak, speak, yield(listen), listen]`; stub `audio_output` with `is_playing` True after `start_generation`, False after `request_stop`, recording both. Assert: start on first speak; on the `yield` chunk while playing → exactly one `request_stop` + a `model_native_barge_in` event (caused_by closed); a yield while NOT playing → no stop; events closed; turn-based suite regression-green. A second unit test asserts `type(model._duplex)` is **restored** to the original class after `stream_chunks` returns (gate-relax doesn't leak). Do NOT import `MiniCPMNativeTtsAdapter` (pulls torch — CPU-impossible); `_duplex_tts` isolation is structural, not asserted. Real-model gate-relax behavior + the N≥20 re-probe are manual/b200, recorded in the PR.

## 5. Invariants

- **#8:** barge-in stops speech (favors silence); never forces speech.
- **#2/#4:** speech still starts only on a `decide_chunk` speak decision.
- **#1/#10:** `model_native_barge_in` logged with `caused_by`; non-blocking.

## 6. Risks / open questions

| Item | Handling |
|---|---|
| `__class__` swap leaks if an exception skips restore | inner `try/finally` wraps `prepare()` + loop; restore always runs (§1). |
| Real-model coherence of mid-turn yield | probe §3.1/§3.2 GO; PR3b real-model run manual/b200. |
| `request_stop` exact signature when wiring live | use the real `AudioOutputController.request_stop`; stub mirrors it. |
| BC veto absent | PR3c adds the veto; until then a model yield always stops (PR3b = model-native barge-in; discrimination is PR3c). |

## 7. Out of scope

BC veto + VAD safety-net + N≥20 re-probe + state-(b) demotion (PR3c); real TTS/live wiring (later).
