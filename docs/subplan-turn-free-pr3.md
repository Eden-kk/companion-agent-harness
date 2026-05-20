# Sub-plan: PR3 — Model-native barge-in + BackchannelClassifier veto

**Status:** DRAFT (round 0) — for plan-critic review.
**Parent:** [`plan-turn-free-continuous-execution.md`](plan-turn-free-continuous-execution.md) PR3.
**Stacks on:** `feat/turn-free-pr2` (uses PR2's `decide_chunk` + `PerChunkPolicyInputs`, and PR1's `ContinuousOrchestrator` + `stream_chunks`).
**One outcome:** the continuous orchestrator acts on the model's own per-chunk `is_listen` as the **primary** barge-in signal — relaxing the mid-turn gate so the model *can* yield — with VAD/SmartTurn demoted to safety-net and the BackchannelClassifier as a confirmatory veto. Ships in **state (a)** (veto active); demotion to state (b) is gated by the N≥20 real-audio re-probe (not this PR's merge).

---

## 1. The gate-relax problem (read first)

The mid-turn yield gate is in the **HF model file** `modeling_minicpmo.py:3215` (`if last_id == listen_token_id and not current_turn_ended: → keep speaking`). That file is downloaded via `trust_remote_code` — **the harness cannot edit it.** The probe (`scripts/probe_turn_gate_barge_in.py`) relaxed it at runtime with an `_AlwaysEnded` data-descriptor on `current_turn_ended`. PR3 does the same, but:
- applied by the **adapter** (`MiniCPMStreamingModel`), **only in the continuous path** (a `continuous=True` / `relax_turn_gate=True` flag), so the turn-based path keeps the gate;
- installed in `stream_chunks()` setup, removed on teardown (try/finally), so it never leaks into a turn-based session sharing the process.

This is the one genuinely model-coupled piece; it stays inside the adapter (adapter-first).

## 2. Wire `decide_chunk` into the orchestrator (replace the PR1 placeholder)

PR1's `_policy_hook` is `pass`. PR3 replaces it: build a `PerChunkPolicyInputs` from the chunk + available signals, call `decide_chunk`, act on the `SpeakDecision`.

```
run() loop (per chunk, extending PR1):
    is_listen, text, audio_kv_len, caused_by_evt_id = <from stream_chunks>
    emit continuous_chunk_processed (PR1)                       # unchanged
    inputs = PerChunkPolicyInputs(chunk_index, model_is_listen=is_listen,
                                  backchannel_score=<bc>, user_addressed_agent=<addr>,
                                  privacy_mode, social_mode, budget_full_response_remaining)
    decision = decide_chunk(inputs, caused_by_evt_id=caused_by_evt_id)
    emit policy_decision (action_type + primary_reason_code, caused_by)   # NEW
    route(decision, is_listen, text, caused_by_evt_id)
```

`backchannel_score` and `user_addressed_agent`: PR3 sources these from the **BackchannelClassifier** (veto, §4) and an addressing signal. To stay CPU-testable and minimal, PR3 wires `backchannel_score` from the classifier and defaults `user_addressed_agent=True` in continuous mode (a continuous companion is being addressed by construction; refined later). Other `PerChunkPolicyInputs` fields stay at PR2 defaults until their producers are wired.

## 3. Barge-in routing (model-native primary)

The model's per-chunk `is_listen` is the primary turn-taking signal (gate relaxed, so it can yield mid-utterance):

```
route(decision, is_listen, text, evt):
    if assistant_is_speaking:
        if is_listen and not _bc_veto(): # model yielded mid-speech → barge-in
            audio_output.stop(caused_by=[evt]); emit model_native_barge_in(evt)
        # else: keep speaking (model still wants the floor, or BC veto held it)
    else:
        if decision.action_type in SPEAK_ACTIONS:
            audio_output.start(decision, text, caused_by=[evt])   # begin speaking
```

- **Primary stop signal = the model's `is_listen` yield**, not VAD. VAD/SmartTurn remain running (PR1 feed) as a **safety net**: a `safety_net_barge_in` path fires `audio_output.stop()` only if the model has NOT yielded within a bounded number of chunks after detector-confirmed user speech. (Keeps the spec's <200 ms guarantee if the model is slow.)
- `audio_output` is an **injected `AudioOutputController`-shaped adapter** (start/stop), stubbed in the CPU test. PR3 does not require real TTS to validate the routing.

## 4. BackchannelClassifier as confirmatory veto (state a)

`_bc_veto()` returns True when the latest `backchannel_score >= threshold` — i.e., the overlapping user audio is a backchannel ("mm-hmm"), so a model yield should be **suppressed** (keep speaking). Wiring: the BackchannelClassifier (already in the harness) scores recent audio; the orchestrator reads its score per chunk and feeds it into both `PerChunkPolicyInputs.backchannel_score` and `_bc_veto()`.

**State (a) — what PR3 ships:** gate relaxed, model-`is_listen` primary, VAD safety-net active, BC veto active.
**State (b) — deferred:** after the **N≥20 real-human-audio re-probe** (GO criterion: interruption yield-rate ≥80% AND backchannel false-yield ≤10%, per the parent plan), the BC veto demotes to logging-only and VAD to safety-net-only. This is a follow-on sub-change gated by the probe — **not part of PR3's merge.**

## 5. File-by-file

| File | Change |
|---|---|
| `companion_harness/foreground_model_minicpm.py` | Add the `current_turn_ended` runtime relax (the `_AlwaysEnded` descriptor) inside `stream_chunks()` setup when `relax_turn_gate=True`; remove in `finally`. Confined to continuous path; turn-based untouched. |
| `companion_harness/continuous_orchestrator.py` | Replace `_policy_hook` placeholder: build `PerChunkPolicyInputs`, call `decide_chunk`, emit `policy_decision`, route to `audio_output` start/stop. Add model-native barge-in + VAD safety-net + `_bc_veto`. Ctor gains injected `audio_output` + `backchannel_source`. |
| `companion_harness/v0_1g_event_schema.py` | Register `policy_decision` (if not already), `model_native_barge_in`, `safety_net_barge_in`. |
| `tests/test_continuous_barge_in.py` (new) | CPU success test (§6). |

## 6. Success criterion (sole programmatic gate)

`tests/test_continuous_barge_in.py::test_continuous_barge_in_and_backchannel` — CPU-runnable with stubs (`FakeForegroundModel` from PR1 extended to script an is_listen sequence; stub `audio_output` recording start/stop; stub backchannel source). Asserts:
- model speaks (`audio_output.start`) when `decide_chunk → full_response`;
- a mid-speech model yield (`is_listen` True after speaking) with low backchannel score → `audio_output.stop` + a `model_native_barge_in` event (caused_by closed);
- a mid-speech model yield with **high** backchannel score → **no stop** (BC veto held the floor);
- all emitted events have closed `caused_by[]`; turn-based suite regression-green.

Real-model gate-relax behavior + the N≥20 real-audio re-probe are manual/b200, recorded in the PR, **not** the CPU gate.

## 7. Invariants

- **#2/#4:** every speak action still goes through `decide_chunk` → `SpeakDecision`; `audio_output.start` only on a speak decision.
- **#5:** `decide_chunk` stays pure/deterministic (PR2); the orchestrator's routing is deterministic given the recorded `(PerChunkPolicyInputs, is_listen, backchannel_score)` sequence.
- **#8:** silence wins ties — barge-in *stops* speech (favoring silence); BC veto only *prevents* a stop, never *forces* speech.
- **#1/#10:** `policy_decision` + barge-in events logged with `caused_by`; non-blocking.

## 8. Risks / open questions

| Item | Handling |
|---|---|
| Descriptor relax leaking into a turn-based session in the same process | Install/remove in `stream_chunks()` try/finally; never global. Test asserts the turn-based gate path is unchanged. |
| `audio_output` adapter shape | Reuse the existing `AudioOutputController` interface (start_generation/stop) so PR-later live wiring drops in; stub it in the test. |
| `user_addressed_agent=True` default is a simplification | Acceptable for PR3 (continuous companion is addressed by construction); a real addressing signal wires in a later PR. Note in code. |
| VAD safety-net timing | Bounded "model hasn't yielded within K chunks of detector-confirmed speech" → safety stop. K is a constant in PR3; tuned in PR5a. |

## 9. Out of scope (later PRs)

Real TTS dispatch wiring to the live server (PR-later), the N≥20 real-audio re-probe + state-(b) demotion (gated follow-on), background-think injection (PR4), chunk_ms/listen_prob tuning (PR5a).
