# Subplan — PR2: TactMiniCPMDriver (the one model-coupled slice)

Design brief for the only MiniCPM-coupled piece. Keep all model-SDK imports here.

## Decisions (grounded in the actual framework, correcting the master plan)

1. **Event mechanism = in-memory event list + `_make_event`, NOT the realtime
   EventLogger.** The canonical eval driver (`evals/scenarios/fixture.py`) uses an
   `event_sink: list[Event]` + a `_make_event` helper. Match it (CLAUDE.md: match
   existing style). The realtime EventLogger is for the live path, not eval drivers.

2. **The model decision is data, not events.** Like `fixture.py` (whose events are
   bookkeeping, not the orchestrator's behavior), the per-chunk `is_listen`/`text`
   trajectory + the user-speaking mask go into `ReplayRun.results`, so metrics (PR4)
   are pure functions of the run. Only bookkeeping is emitted as Events.

3. **Events emitted (DAG closes; every Event has `caused_by[]`):**
   - `benchmark_case_started` (caused_by `[]`) — exists in EVAL_EVENT_TYPE_SCHEMAS
   - `held_result_injected` (caused_by `[started]`) — **NEW type** (register)
   - `benchmark_case_completed` (caused_by `[started, held_result_injected]`)
   Per-chunk events are intentionally omitted (would be noise; trajectory is in results).

4. **`held_result_injected` schema** (register in `evals/schemas.py`
   EVAL_EVENT_TYPE_SCHEMAS): `payload_kind="transcript"`, `subject_class="self"`,
   `sensitivity="sensitive"`, `retention_policy_id="eval_run_30d"` (the eval bucket the
   other eval events use; eval ids are not yaml-enforced — verified). Only the payload
   **hash** is stored in the Event (fixture pattern), so no raw free text lands in the
   log (satisfies the SensitiveField intent).

5. **Model load is lazy + once.** `MiniCPMStreamingModel` loads on first `run()` and is
   reused across cases (the driver instance is shared by the runner). A `model_factory`
   hook allows injecting a fake in tests so PR2's test needs no GPU.

6. **Scope = text/silence-clock only.** Port `_build_text_plan` / `_held_result_turn`
   (with the R2 URGENT guard) / `_user_turn` / `_generate` / silence-stepping from
   `scripts/probe_tact_vanilla_vs_prompted.py` into the driver module. Audio mode = PR7.
   Duplicate now (surgical); the probe is retired at parity (master plan OQ2).

7. **Event log persistence.** Serialize the event list to
   `run_config.output_dir/event_logs/<case_id>.jsonl`; set `ReplayRun.event_log_path`.

## ReplayRun shape
```
results = {
  "arm": "prompted"|"vanilla",
  "input_mode": "text",
  "trajectory": [{t, injected, is_listen, text}, ...],
  "speaking_mask": [bool, ...],
  "item": {...}, "expected_behavior": {...},   # carried for metrics convenience
}
event_log_path = .../event_logs/<case_id>.jsonl
final_status = "completed" | "error"
timing_mode = "synthetic_clock"
```

## Success criterion (PR2)
`tests/test_tact_driver.py` (fake model, no GPU): `run()` on one case yields a
ReplayRun whose event log contains `benchmark_case_started`, `held_result_injected`,
`benchmark_case_completed` (all with valid `caused_by`), and whose `results.trajectory`
has one entry per chunk with the injection flagged at `t_available`. A separate
GPU-gated smoke (`--limit 1` real model) is run manually, not in CI.

## Risks
- Fake-model fidelity: the fake must mimic `_duplex.streaming_prefill/streaming_generate`
  + the attrs `_generate` reads. Mirror the real signature exactly.
- Shared duplex session state across cases → reset_session + prepare per case (the probe
  already does this per arm).
