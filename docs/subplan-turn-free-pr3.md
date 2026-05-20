# Sub-plan: PR3a — Wire decide_chunk into the orchestrator (act on per-chunk decisions)

**Status:** DRAFT (round 1, re-scoped) — for plan-critic review.
**Parent:** [`plan-turn-free-continuous-execution.md`](plan-turn-free-continuous-execution.md) PR3.
**Stacks on:** `feat/turn-free-pr2`.
**One outcome:** the continuous orchestrator builds a `PerChunkPolicyInputs` per chunk, calls PR2's `decide_chunk`, emits a `policy_decision` event, and starts speech (via an injected `audio_output` adapter) on a speak decision — replacing PR1's `pass` placeholder.

### Why PR3 is split (plan-critic round 0 found PR3 too big, with model-coupled + undefined seams)

The original PR3 bundled five things; three had blockers. It is split:
- **PR3a (this sub-plan):** wire `decide_chunk` + act on decisions (start speech). No gate-relax, no barge-in stop, no BC veto, no VAD. CPU-testable with stubs.
- **PR3b (next sub-plan):** model-native barge-in — relax the mid-turn gate **instance-safely** (a class-level descriptor breaks the native-TTS path, which sets `current_turn_ended=False` on a sibling duplex — `tts_minicpm_native.py:163`; PR3b will subclass *only* `self._duplex`'s type and restore on teardown) + stop speech on a model `is_listen` yield.
- **PR3c (later sub-plan):** BackchannelClassifier confirmatory veto (needs a per-chunk pull interface — the classifier is per-frame push today) + VAD safety-net (needs a vad-signal producer into the orchestrator ctor). The N≥20 real-audio re-probe gates the state-(b) demotion.

PR3a is the foundation the other two build on: you cannot barge into speech that is never dispatched.

---

## 1. The decision-wiring (replace the placeholder)

PR1's `_policy_hook` is `pass`. PR3a replaces the per-chunk tail of `run()`:

```
per chunk (extending PR1's loop):
    is_listen, text, audio_kv_len, caused_by_evt_id = <from stream_chunks>
    emit continuous_chunk_processed (PR1)                       # unchanged
    inputs = PerChunkPolicyInputs(
        chunk_index=<seq>, model_is_listen=is_listen,
        backchannel_score=0.0,            # BC source wired in PR3c; 0.0 until then
        user_addressed_agent=True,        # continuous companion is addressed by construction; refined later
        privacy_mode=self._privacy_mode, social_mode=self._social_mode,
        budget_full_response_remaining=self._budget_full_response_remaining)
    decision = decide_chunk(inputs, caused_by_evt_id=caused_by_evt_id)
    emit policy_decision (payload_inline {action_type, primary_reason_code}, caused_by=[caused_by_evt_id])  # NEW
    self._act(decision, caused_by_evt_id)
```

`privacy_mode`/`social_mode`/`budget_full_response_remaining` are ctor params (defaults: `"normal"`, `"user_addressing_agent"`, `1`) — they're the modes the continuous session runs under; not signal producers, just config. No new producers are needed for PR3a.

## 2. Acting on the decision (start speech only — no stop in PR3a)

```
_act(decision, evt):
    SPEAK_ACTIONS = {"full_response", "backchannel", "short_reaction",
                     "clarification", "alert", "tool_status", "aesthetic_reaction"}
    if decision.action_type in SPEAK_ACTIONS and not self._audio_output.is_playing:
        self._audio_output.start_generation(caused_by=[evt])    # begin speaking
    # action_type == "silence": nothing.
    # already speaking (is_playing): nothing — PR3b adds the barge-in stop path.
```

- **`assistant_is_speaking` is read from the adapter** (`self._audio_output.is_playing`), not a state machine the orchestrator owns — mirroring `realtime_orchestrator.py`'s use of `self._audio_output.is_playing`. The adapter owns playback lifecycle; PR3a only *starts*.
- **All `decide_chunk` action types are handled:** every speak action → start; `silence` → nothing. (PR2's `decide_chunk` currently returns only `silence`/`full_response`/`backchannel`; the set is future-proof but the branch is a single membership test, not per-action code — no speculative per-action logic.)

## 3. The `audio_output` adapter (injected, stubbed in test)

`ContinuousOrchestrator.__init__` gains an injected `audio_output` typed to a minimal Protocol:

```python
class _AudioOutputProtocol(Protocol):
    @property
    def is_playing(self) -> bool: ...
    def start_generation(self, *, caused_by: list[str]) -> str: ...
```

This is the subset of the real `AudioOutputController` interface PR3a needs (the real one has these). PR3b adds `stop`/`cancel` to the Protocol when it needs them. The CPU test injects a stub exposing `is_playing` (test-controlled) + a recording `start_generation`.

## 4. File-by-file

| File | Change |
|---|---|
| `companion_harness/continuous_orchestrator.py` | Replace `_policy_hook` placeholder with `_act` (§2); build `PerChunkPolicyInputs` + call `decide_chunk` + emit `policy_decision` in `run()`; ctor gains injected `audio_output` + `privacy_mode`/`social_mode`/`budget_full_response_remaining` params + `_AudioOutputProtocol`. Imports `decide_chunk`, `PerChunkPolicyInputs`. |
| `companion_harness/v0_1g_event_schema.py` | **Add `policy_decision`** — it is ABSENT today (critic-confirmed; only referenced in a prose note). Register in `EVENT_TYPE_SCHEMAS` + `EXPECTED_EVENT_TYPES` with `payload_kind="signal"`, `subject_class="self"`, `sensitivity="safe"`, `retention_policy_id="signal_default_30d"`. |
| `tests/test_continuous_pr3a_act.py` (new — distinct file) | CPU success test (§5). |

## 5. Success criterion (sole programmatic gate)

`tests/test_continuous_pr3a_act.py::test_decide_chunk_wired_and_starts_speech` — CPU-runnable, no GPU/weights. Define a local `FakeForegroundModel` (inline in this test file; do NOT import or modify PR1's test fixture) whose `stream_chunks` yields a scripted sequence of `(is_listen, text, audio_kv_len, evt_id)`, and a stub `audio_output` with a test-settable `is_playing` + a recording `start_generation`. Assert:
- a chunk with `is_listen=False` (model wants to speak) + `is_playing=False` → exactly one `start_generation` call + a `policy_decision` event with `action_type="full_response"`;
- a chunk with `is_listen=True` → no `start_generation`, `policy_decision` `action_type="silence"`;
- a speak chunk while `is_playing=True` → no second `start_generation` (no double-start);
- every emitted event (`continuous_chunk_processed`, `policy_decision`) has closed `caused_by[]`;
- turn-based suite regression-green.

## 6. Invariants

- **#2/#4:** speech starts only via a `decide_chunk` `SpeakDecision` speak action; `silence` never starts speech.
- **#5:** `decide_chunk` is pure (PR2); the `_act` routing is a deterministic function of `(decision, is_playing)`.
- **#8:** silence wins ties — PR3a only *starts* on an explicit speak decision; ambiguity → silence → nothing.
- **#1/#10:** `policy_decision` logged per chunk with `caused_by`; non-blocking.

## 7. Risks / open questions

| Item | Handling |
|---|---|
| `start_generation` returns a `gen_event_id` (`realtime_orchestrator.py:1486`) used as `caused_by` for downstream stop/TTS events | PR3a has no stop path so it ignores the return value — but **PR3b must capture `gen_event_id`** and thread it as `caused_by` for the barge-in stop. Noted so the PR3a call site is written knowing PR3b extends it. |
| `is_playing` never clears in PR3a (no completion signal) | The injected adapter owns playback lifecycle + `is_playing`; PR3a doesn't manage it. The stub controls it in the test. Real completion/stop is PR3b (barge-in) + live-wiring. |
| `_AudioOutputProtocol` vs real `AudioOutputController` | Use the real interface's method names (`is_playing`, `start_generation`) so the live adapter drops in unchanged. |
| Future-proof SPEAK_ACTIONS set | A single membership test, not per-action code — not speculative. |

## 8. Out of scope (PR3b / PR3c / later)

Gate-relax + model-native barge-in stop (PR3b); BackchannelClassifier veto + VAD safety-net + the N≥20 re-probe + state-(b) demotion (PR3c); real TTS/live-server wiring (later). PR3a is decision-wiring + start-on-speak, nothing more.
