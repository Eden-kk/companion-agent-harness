# Plan — memory wiring follow-up (manual-test live pipeline)

## Status: **DRAFT** — converged 2026-05-15 (recovered + patched per memory plan-critic round 1).

> PR #125 wired the four-store memory architecture but left
> `episodic_store=None, semantic_store=None` in the manual-test live
> pipeline factory. This plan finishes that wiring so manual-test
> sessions exercise the real provenance path and the v0.1e contract
> tests for cross-adapter retrieval are mirrored by a live-pipeline
> equivalent.

## Goal

Wire per-session memory stores into the manual-test live pipeline so
video and audio events land in the four-store memory architecture
with full provenance, and retrieved items reach the foreground model
on a per-call basis (no cross-session leakage).

## Anchors (all RESOLVED 2026-05-15)

### Anchor 1 — MiniCPM context concurrency: pass `context_items` per call

Extend `DuplexModel.process_stream(frame_iter, caused_by,
context_items=())` Protocol with a keyword-only default `()`. The
existing `set_context()` becomes an **advisory no-op stash**
(preserves v0.1e contract tests:
`test_retrieved_items_reach_foreground_context`,
`test_set_context_replaces_not_appends`,
`test_set_context_empty_clears`).

**Dual-write during transition:** orchestrator calls BOTH
`self._foreground_model.set_context(retrieved_items)` AND stashes
`self._pending_retrieved_items = retrieved_items`; the per-call value
is what MiniCPM consumes.

**Ordering contract:**

1. T2 sets `_pending_retrieved_items` BEFORE `_batch_open_event.set()`.
2. T3 reads AFTER `await _batch_open_event.wait()`.
3. T3 clears `_pending_retrieved_items = []` before
   `_batch_close_event.set()`.

Single-event-loop guarantees no race.

### Anchor 2 — Operator-managed cleanup

No automatic cleanup. Hygiene note added to
`docs/manual-test-handbook.md`:

```bash
rm -rf /tmp/manual_test_blobs/*   # before each session
```

### Anchor 3 — 4 stores per session, SleepTimeAgent unwired

Layout:

```
<blob_dir>/<session_id>/memory/{session,core,episodic,semantic}/
```

SleepTimeAgent stays unwired in the live pipeline;
`memory_write_candidate` events land in the event log without on-disk
commit. Retrieval queries `episodic` + `semantic` only.

## Implementation sketch

1. **`companion_harness/foreground_model.py`.** Extend
   `DuplexModel.process_stream(...)` Protocol with
   `context_items: tuple[MemoryItem, ...] = ()` keyword. Preserve
   existing fake compatibility.

2. **`companion_harness/foreground_model_minicpm.py:_gen()`.** After
   `duplex.prepare(...)` and before the audio loop, if `context_items`
   is non-empty, fold each item's `user_visible_summary.value` into a
   numbered "Recent context:" preamble; concatenate with the existing
   `prefix_system_prompt`; re-call
   `duplex.prepare(prefix_system_prompt=combined)` if mid-session
   re-prepare is supported, else document the first-call-only
   limitation. Commit to templating-style integration; file a
   follow-up issue if MiniCPM does not condition on the prompt.

3. **`companion_harness/realtime_orchestrator.py:418`.** Keep
   `self._foreground_model.set_context(retrieved_items)` (advisory).
   Add `self._pending_retrieved_items = retrieved_items` stash BEFORE
   `_batch_open_event.set()`. T3 reads, then clears, AFTER batch close.

4. **`manual_test_console/live_pipeline.py:build_live_pipeline()`.**
   Construct 4 per-session stores under
   `<blob_dir>/<session_id>/memory/{session,core,episodic,semantic}/`.
   Pass `episodic_store`, `semantic_store` to orchestrator.

5. **`manual_test_console/server.py`.** Forward `blob_dir` arg.

6. **`docs/manual-test-handbook.md`.** Add hygiene note
   (operator-managed cleanup).

## Test plan

- **`test_per_session_memory_isolation`.** Distinct `LivePipeline`
  instances hold distinct store object identities:

  ```python
  assert id(pipeline_a.session_state_store) != id(pipeline_b.session_state_store)
  ```

- **`test_live_pipeline_passes_context_items_per_call`.** Assert
  `_pending_retrieved_items` flows from T2 → T3 → MiniCPM call.

- **`test_explicit_remember_event_chain_in_live_pipeline`.** Monkeypatch
  `companion_harness.realtime_orchestrator._detect_explicit_remember`
  to return `(True, "extracted")`; assert the event chain closes
  through `memory_write_candidate`.

## Success criterion

```
pytest -k "per_session_memory_isolation or context_items_per_call or explicit_remember_event_chain_in_live_pipeline"
```

passes under the canonical venv.

## §Out of scope

- SleepTimeAgent live-loop wiring.
- VisionSidecar wiring (see `docs/plan-vision-sidecar-wiring.md`).
- `DecisionTrace.retrieval_used` extension to session/core stores.
- Retrieval query string population (waits for ASR transcript).

## §Cross-references

- Issue #113 — log-chain completeness.
- `docs/roadmap-v0.1e-draft.md` §Task 11.
- `tests/test_cross_adapter_retrieval.py`.
