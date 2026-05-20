# Sub-plan: PR3c — BackchannelClassifier confirmatory veto

**Status:** DRAFT (round 1, post-critic) — for plan-critic re-review.
**Parent:** `plan-turn-free-continuous-execution.md` PR3 (split). **Stacks on:** `feat/turn-free-pr3b`.
**One outcome:** a model-native barge-in (PR3b) is **suppressed** when the overlapping user audio is a backchannel ("mm-hmm") — the BackchannelClassifier acts as an orchestrator-level confirmatory veto so the assistant keeps the floor through acknowledgements.

**Scope:** BC veto ONLY. VAD safety-net deferred (needs the detector path wired into the continuous loop — separate PR). N≥20 re-probe → state-(b) demotion deferred (probe-gated).

## 1. Per-chunk backchannel pull interface

The real `BackchannelClassifier` is **per-frame push** (`process_frame`); the orchestrator is **per-chunk pull**. PR3c defines a thin pull Protocol polled once per chunk:

```python
class _BackchannelSourceProtocol(Protocol):
    def latest_score(self) -> float: ...   # most-recent backchannel probability [0,1]
```

Injected into `ContinuousOrchestrator.__init__` as `backchannel_source`, default a module-level `_NullBackchannelSource` returning `0.0` (preserves PR3b behavior when unwired). A null-object **class** is correct here (the Protocol is a *method* `latest_score()`, not a bare callable — a lambda can't satisfy it; null-object avoids None-guards at every call). The live adapter wrapping the real classifier (caching its last `process_frame` score → `latest_score()`) is **live-only wiring, out of scope here**.

## 2. The veto — orchestrator-level only, NOT fed into decide_chunk

**Critical (round-0 blocker 2):** do NOT feed `latest_score()` into `PerChunkPolicyInputs.backchannel_score`. PR2's `decide_chunk` has a branch that returns `action_type="backchannel"` when `backchannel_score >= 0.7` — feeding a high score there would make the gate emit unsolicited assistant-backchannel *speech while the model is already talking*. The continuous model decides speak/silence purely via `model_is_listen`; the BC score is **only** an orchestrator-level suppressor of a model-native barge-in. So `PerChunkPolicyInputs.backchannel_score` stays `0.0` (decide_chunk unchanged), and the veto reads the score in the orchestrator.

**Single read (round-0 blocker 1):** read the score ONCE per loop iteration in `run()` and pass it to `_act` (no double-sampling):

```python
# run(), per chunk:
bc_score = self._backchannel_source.latest_score()
decision = decide_chunk(inputs, caused_by_evt_id=evt)   # inputs.backchannel_score stays 0.0
self._emit_policy_decision(decision, evt)
self._act(decision, is_listen, bc_score, evt)

def _act(self, decision, is_listen, bc_score, evt):
    speaking = self._audio_output.is_playing
    if is_listen and speaking:
        if bc_score >= _BACKCHANNEL_THRESHOLD:
            self._emit_barge_in_suppressed(evt)          # backchannel → keep the floor
            return
        self._audio_output.request_stop(caused_by=[evt])
        self._emit_barge_in(evt)                          # model_native_barge_in (PR3b)
        return
    if decision.action_type in _SPEAK_ACTIONS and not speaking:
        self._audio_output.start_generation(caused_by=[evt])
```

`_BACKCHANNEL_THRESHOLD`: **import it explicitly** from `companion_harness.continuous_speak_policy` (single source of truth) — `from companion_harness.continuous_speak_policy import _BACKCHANNEL_THRESHOLD` (single-underscore import is intentional).

## 3. State (a) vs (b)

PR3c ships **state (a)** (veto active). **State (b)** (veto → logging-only) is gated by the N≥20 real-human-audio re-probe (GO: interruption yield-rate ≥80% AND backchannel false-yield ≤10%) — deferred, NOT this PR's merge.

## 4. File-by-file

| File | Change |
|---|---|
| `companion_harness/continuous_orchestrator.py` | `_BackchannelSourceProtocol` + `_NullBackchannelSource` (returns 0.0); ctor `backchannel_source=_NullBackchannelSource()`; import `_BACKCHANNEL_THRESHOLD`; read `bc_score` once in `run()`, pass to `_act`; `_act` gains `bc_score` param + veto branch + `_emit_barge_in_suppressed`. **Do NOT change `PerChunkPolicyInputs.backchannel_score` (stays 0.0).** |
| `companion_harness/v0_1g_event_schema.py` + `tests/test_v0_1g_event_schema.py` | register `barge_in_suppressed_backchannel` (signal/self/safe/signal_default_30d). If `StageSixEventSchema` has a `required_fields` attribute, populate it (`["backchannel_score"]` if the payload carries it, else omit per the dataclass shape — check the dataclass). |
| `tests/test_continuous_pr3c_bc_veto.py` (new) | CPU success test (§5). |

## 5. Success criterion (sole programmatic gate)

`tests/test_continuous_pr3c_bc_veto.py::test_backchannel_vetoes_barge_in` — CPU, stubs. Stub `backchannel_source` with a settable score; stub `audio_output`. Script: model speaks, then yields (`is_listen=True`) while playing. Assert: `latest_score()=0.9` → NO `request_stop`, a `barge_in_suppressed_backchannel` event (caused_by closed), still playing; `latest_score()=0.0` → `request_stop` + `model_native_barge_in` (PR3b preserved). A third case with the DEFAULT null source (no `backchannel_source` arg) → PR3b stop behavior (regression). All events closed; PR1/PR2/PR3a/PR3b suites regression-green. (`_emit_policy_decision` still emits `silence` on the yield chunk — assert decide_chunk behavior unchanged, proving blocker-2 is avoided.)

## 6. Invariants

- **#8:** veto only prevents a stop; never forces speech.
- **#2/#4:** `decide_chunk` unchanged (BC score not fed to it).
- **#1/#10:** `barge_in_suppressed_backchannel` logged with `caused_by`; non-blocking.

## 7. Risks / open questions

| Item | Handling |
|---|---|
| `latest_score()` staleness (pull vs frame-push) | acceptable (chunk ≫ frame cadence; live adapter caches last). Single read per chunk. |
| Default null source changes behavior? | No — 0.0 < threshold → no veto → identical to PR3b. Regression-tested. |
| `decide_chunk` backchannel branch dead in continuous path | Out of scope for PR3c (it's PR2 code; never triggered since backchannel_score stays 0.0). Noted; a future PR may prune it. |
| VAD safety-net absent | deferred (detector-path wiring); model-native barge-in is the stop mechanism. |

## 8. Out of scope

VAD safety-net; live BC-classifier→latest_score adapter; N≥20 re-probe + state-(b) demotion; PR4.
