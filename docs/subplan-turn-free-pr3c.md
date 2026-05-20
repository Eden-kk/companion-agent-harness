# Sub-plan: PR3c — BackchannelClassifier confirmatory veto

**Status:** DRAFT (round 0) — for plan-critic review.
**Parent:** `plan-turn-free-continuous-execution.md` PR3 (split). **Stacks on:** `feat/turn-free-pr3b`.
**One outcome:** a model-native barge-in (PR3b) is **suppressed** when the overlapping user audio is a backchannel ("mm-hmm") — the BackchannelClassifier acts as a confirmatory veto so the assistant keeps the floor through acknowledgements.

**Scope decision:** PR3c is **BC veto only**. The VAD safety-net (design §6) is **deferred** — it needs the detector path wired into the continuous loop (PR1's orchestrator consumes only `stream_chunks`, not VAD signals), a separate, larger change. The N≥20 real-audio re-probe → state-(b) demotion is also deferred (operator/probe-gated).

## 1. Per-chunk backchannel pull interface

The real `BackchannelClassifier` is **per-frame push** (`process_frame(frame, caused_by)`); the orchestrator is **per-chunk pull**. PR3c defines a thin pull Protocol the orchestrator polls once per chunk:

```python
class _BackchannelSourceProtocol(Protocol):
    def latest_score(self) -> float: ...   # most-recent backchannel probability [0,1]
```

Injected into `ContinuousOrchestrator.__init__` as `backchannel_source`, **optional, default a null source returning `0.0`** (so PR3a/PR3b behavior is unchanged when no source is wired). A live adapter wrapping the real classifier (caching the last `process_frame` score → `latest_score()`) is **live-only wiring, out of scope here**; PR3c ships the Protocol + the orchestrator's use + a stub.

## 2. The veto + feeding `backchannel_score`

PR3a hardcodes `backchannel_score=0.0` in `run()`. PR3c replaces that with `self._backchannel_source.latest_score()` (so `decide_chunk` sees the real score too). In `_act`, the barge-in branch gains the veto:

```python
def _act(self, decision, is_listen, caused_by_evt_id):
    speaking = self._audio_output.is_playing
    if is_listen and speaking:
        if self._backchannel_source.latest_score() >= _BACKCHANNEL_THRESHOLD:
            self._emit_barge_in_suppressed(caused_by_evt_id)   # backchannel → keep the floor
            return
        self._audio_output.request_stop(caused_by=[caused_by_evt_id])
        self._emit_barge_in(caused_by_evt_id)                  # model_native_barge_in (PR3b)
        return
    if decision.action_type in _SPEAK_ACTIONS and not speaking:
        self._audio_output.start_generation(caused_by=[caused_by_evt_id])
```

`_BACKCHANNEL_THRESHOLD = 0.7` (reuse the continuous_speak_policy value; import or re-declare a module constant — single source preferred). Silence still wins ties (#8): the veto only *prevents a stop*, never forces speech.

## 3. State (a) vs (b)

PR3c ships **state (a)**: veto active (a model yield is suppressed on a backchannel). **State (b)** — demote the veto to logging-only once model-native discrimination is validated at **N≥20 real human audio** (GO: interruption yield-rate ≥80% AND backchannel false-yield ≤10%) — is a deferred, probe-gated follow-on, NOT this PR's merge.

## 4. File-by-file

| File | Change |
|---|---|
| `companion_harness/continuous_orchestrator.py` | `_BackchannelSourceProtocol` + a module-level `_NullBackchannelSource` (returns 0.0); ctor `backchannel_source: _BackchannelSourceProtocol = _NullBackchannelSource()`; feed `latest_score()` into `PerChunkPolicyInputs.backchannel_score` (replace the 0.0); `_act` veto branch + `_emit_barge_in_suppressed`. |
| `companion_harness/v0_1g_event_schema.py` + `tests/test_v0_1g_event_schema.py` | register `barge_in_suppressed_backchannel` (signal/self/safe/signal_default_30d). |
| `tests/test_continuous_pr3c_bc_veto.py` (new) | CPU success test (§5). |

## 5. Success criterion (sole programmatic gate)

`tests/test_continuous_pr3c_bc_veto.py::test_backchannel_vetoes_barge_in` — CPU, stubs. Stub `backchannel_source` with a test-settable score; stub `audio_output` (is_playing). Script: model speaks, then yields (`is_listen=True`) while playing. Assert: with `latest_score()=0.9` → NO `request_stop`, a `barge_in_suppressed_backchannel` event (caused_by closed), assistant still playing; with `latest_score()=0.0` → `request_stop` + `model_native_barge_in` (PR3b behavior preserved). Default null source (no `backchannel_source` arg) → PR3b behavior (stop) unchanged — regression test. All events closed; PR1/PR2/PR3a/PR3b suites regression-green.

## 6. Invariants

- **#8:** veto only prevents a stop; never forces speech.
- **#2/#4:** unchanged.
- **#1/#10:** `barge_in_suppressed_backchannel` logged with `caused_by`; non-blocking.

## 7. Risks / open questions

| Item | Handling |
|---|---|
| `latest_score()` pull vs per-frame push cadence (staleness) | acceptable for PR3c (chunk ~200ms–1s; BC score updates per frame ~30ms, so latest_score is fresh-enough). Live adapter caches last score. |
| `_BACKCHANNEL_THRESHOLD` duplicated with continuous_speak_policy | import the constant from `continuous_speak_policy` (single source) rather than re-declare. |
| Default null source changes behavior? | No — null returns 0.0 < threshold → no veto → identical to PR3b. Regression-tested. |
| VAD safety-net absent | deferred (needs detector path in the continuous loop); model-native barge-in (PR3b) is the stop mechanism; safety-net is belt-and-suspenders for the slow-model case, a separate PR. Noted. |

## 8. Out of scope

VAD safety-net (separate PR — detector path wiring); the live BC-classifier→latest_score adapter (live wiring); N≥20 re-probe + state-(b) demotion (probe-gated); PR4 background-think.
