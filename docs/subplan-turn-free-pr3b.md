# Sub-plan: PR3b — Model-native barge-in (gate-relax + stop on yield)

**Status:** DRAFT (round 0) — for plan-critic review.
**Parent:** `plan-turn-free-continuous-execution.md` PR3 (split). **Stacks on:** `feat/turn-free-pr3a`.
**One outcome:** the model's own mid-utterance `is_listen` yield stops the assistant's speech (model-native barge-in), by relaxing the mid-turn gate **instance-safely** and wiring a stop path in the orchestrator.

## 1. Instance-safe gate-relax (the hard part)

The gate is in the HF model file (`modeling_minicpmo.py:3215`); relax it at runtime — but **NOT** with a class-level descriptor (PR3-round-0 blocker: that shadows `self._duplex_tts.current_turn_ended = False` at `tts_minicpm_native.py:163`, breaking the native-TTS remap). Instead, **reassign only `self._duplex`'s `__class__` to a per-instance subclass** carrying the descriptor:

```python
# in MiniCPMStreamingModel.stream_chunks(), when relax_turn_gate=True:
orig_cls = type(self._duplex)
relaxed_cls = type(f"{orig_cls.__name__}_GateRelaxed", (orig_cls,),
                   {"current_turn_ended": _AlwaysEnded()})   # data descriptor: get→True, set→no-op
self._duplex.__class__ = relaxed_cls
try:
    ... existing per-chunk loop ...
finally:
    self._duplex.__class__ = orig_cls
```

Only `self._duplex` is affected; `self._duplex_tts` (a different instance, unchanged class) keeps its `current_turn_ended` behavior. Confined to the continuous path; turn-based untouched. (`__class__` reassignment is safe — `MiniCPMODuplex` has no `__slots__`.)

## 2. `relax_turn_gate` flag

Add `relax_turn_gate: bool = False` to `MiniCPMStreamingModel.stream_chunks(...)` and to `_ForegroundModelProtocol.stream_chunks` (keep adapter-first typing). `ContinuousOrchestrator` passes `relax_turn_gate=True` when it calls `stream_chunks`.

## 3. Barge-in stop routing

`ContinuousOrchestrator._act` gains the stop branch (the model yielding mid-speech is the primary barge-in):

```python
def _act(self, decision, is_listen, caused_by_evt_id):
    speaking = self._audio_output.is_playing
    if is_listen and speaking:                       # model chose listen while we spoke → barge-in
        self._audio_output.stop(caused_by=[caused_by_evt_id])
        self._emit_barge_in(caused_by_evt_id)        # model_native_barge_in event
        return
    if decision.action_type in _SPEAK_ACTIONS and not speaking:
        self._audio_output.start_generation(caused_by=[caused_by_evt_id])
```

`run()` passes `is_listen` to `_act`. `_AudioOutputProtocol` gains `stop(self, *, caused_by) -> None`. Silence wins ties (#8): barge-in *stops* speech.

## 4. File-by-file

| File | Change |
|---|---|
| `companion_harness/foreground_model_minicpm.py` | `stream_chunks(..., relax_turn_gate=False)`; instance-subclass gate-relax in setup + restore in `finally` (§1); add module-level `_AlwaysEnded` descriptor. |
| `companion_harness/continuous_orchestrator.py` | `_act` stop branch (§3); pass `is_listen`; `relax_turn_gate=True` to `stream_chunks`; `stop` on `_AudioOutputProtocol`; `_emit_barge_in`. |
| `companion_harness/v0_1g_event_schema.py` | register `model_native_barge_in` (signal/self/safe/signal_default_30d) + EXPECTED_EVENT_TYPES. |
| `tests/test_continuous_pr3b_barge_in.py` (new) | CPU success test (§5). |

## 5. Success criterion (sole programmatic gate)

`tests/test_continuous_pr3b_barge_in.py::test_model_yield_stops_speech` — CPU, stubs. Script the FakeForegroundModel is_listen sequence `[speak, speak, yield(listen), listen]` with a stub `audio_output` (is_playing True after start, False after stop). Assert: start on first speak; on the `yield` chunk while playing → exactly one `audio_output.stop` + a `model_native_barge_in` event (caused_by closed); a yield while NOT playing → no stop; events closed; turn-based suite regression-green. The instance-safe gate-relax's real-model behavior + the N≥20 real-audio re-probe are manual/b200, recorded in the PR — not the CPU gate. (A unit test asserts the gate-relax restores `self._duplex.__class__` after `stream_chunks` and never touches a second duplex instance.)

## 6. Invariants

- **#8:** barge-in stops speech (favors silence); never forces speech.
- **#2/#4:** unchanged — speech still starts only on a `decide_chunk` speak decision.
- **#1/#10:** `model_native_barge_in` logged with `caused_by`; non-blocking.

## 7. Risks / open questions

| Item | Handling |
|---|---|
| `__class__` reassignment leaking / affecting `_duplex_tts` | Only `self._duplex.__class__` is changed; `_duplex_tts` is a separate instance. A unit test asserts `_duplex_tts`'s class is unchanged during `stream_chunks`. Restore in `finally`. |
| Real-model coherence of mid-turn yield | Probe §3.1/§3.2 already GO; PR3b's real-model run is manual/b200. |
| `stop` semantics vs the real AudioOutputController | Use the real `stop`/`cancel_generation` method name; stub in test. Confirm the exact method when wiring live (PR-later). |
| BC veto not yet present | PR3c adds the veto; until then a model yield always stops (acceptable — PR3b is model-native barge-in; discrimination is PR3c). |

## 8. Out of scope

BC veto + VAD safety-net + N≥20 re-probe + state-(b) demotion (PR3c); real TTS/live wiring (later).
