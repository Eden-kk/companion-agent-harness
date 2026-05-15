# PR Review Ledger — companion-agent-harness spec-gatekeeper

Entries are appended in review order. Each entry records verdict, findings,
and cross-PR watch-items that future PRs must honor.

---

## PR #1 — Implement schemas.py + ReasonCode enum (ROADMAP Task 1)  (reviewed 2026-05-13)
**ROADMAP task:** 1
**Verdict:** APPROVE-WITH-NITS

**Findings:**

- [SHOULD-FIX] `companion_harness/schemas.py:69` — `DecisionTrace.counterfactuals` typed as `dict[str, bool | str]` but spec (Part 5) declares it `dict` with a comment "enum-typed values only". The example shows `True` (bool) and string values — neither is an `Enum` instance — so the comment and the examples contradict each other. Either way, narrowing to `bool | str` is speculative: the type cannot be known until the policy layer is implemented (Task 6). Change to `dict` to match the spec literally and avoid a narrowing that may need to be reverted.

- [SHOULD-FIX] `companion_harness/schemas.py:172` — `ReplayRun.failures` typed as `list[dict[str, str]]` but spec declares it `list[dict]`. The spec comment shows `{check, expected, actual, event_ref}`; `actual` and `expected` will hold numeric measurements (floats/ints), not strings. This narrowing will cause a type error when the ReplayRun is populated in Task 17. Change to `list[dict]` to match spec.

- [NIT] `companion_harness/schemas.py:33–38` — `SensitiveField` dataclass has defaults for every field (`value=None`, `sensitivity="sensitive"`, `retention_policy_id=""`). The spec defines `SensitiveField` as a pure type with no prescribed defaults. Defaults are not wrong per se, but `retention_policy_id=""` is a silent empty string that will pass validation and produce an audited record with no retention policy — a latent correctness hazard. Consider requiring `retention_policy_id` as a positional field (no default) so callers must supply it explicitly.

- [NIT] `companion_harness/schemas.py:17–28` — `__all__` does not include `ReasonCode`, even though `ReasonCode` is imported into this module's namespace and is the type of fields in `SpeakDecision` and `DecisionTrace`. Callers who `from companion_harness.schemas import *` will get the dataclasses but not `ReasonCode`. Either add it to `__all__` or add a module-level comment explaining callers must import `ReasonCode` from `companion_harness.reason_codes` directly.

**Scope check:** PR touches exactly `companion_harness/reason_codes.py` and `companion_harness/schemas.py`. No behavior, no tests modified. Matches Task 1 exactly.

**Invariant checks:**
- `Event.caused_by: list[str]` — present. PASS.
- `SpeakDecision.primary_reason_code: ReasonCode` — present. PASS.
- `SpeakDecision.redacted_explanation: str | None` — correctly NOT `SensitiveField`. Prior decision honored. PASS.
- `MemoryItem` provenance fields (invariant #3): `source_event_id`, `created_at`, `confidence`, `salience`, `valid_from`, `valid_to`, `superseded_by`, `user_visible_summary` — all 8 present. PASS.
- `ReasonCode` enum members: all 10 from spec present, none invented. PASS.

**Cross-PR watch-items created:**
1. `DecisionTrace.counterfactuals` was narrowed to `dict[str, bool | str]` in this PR (deviating from spec's bare `dict`). Task 6 (SpeakPolicy) and Task 14 (policy replay) must produce counterfactual dicts with values that fit `bool | str`; if any value type diverges, the schema must be widened back to `dict`. Flag this when reviewing Task 6.
2. `ReplayRun.failures` was narrowed to `list[dict[str, str]]` (deviating from spec's `list[dict]`). Task 17 (ReplayRun report) must confirm `actual`/`expected` values are always strings; if not, this must be fixed before Task 17 ships.
3. `SensitiveField.retention_policy_id` has a default of `""` — future PRs that construct `SensitiveField` instances must never leave it as the empty string in production paths.

**Cross-PR watch-items checked:** none — first review.

---

## PR #1 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 1
**Verdict:** APPROVE

**Fixes verified (commit 5e34f98):**

- [SHOULD-FIX #1 — RESOLVED] `DecisionTrace.counterfactuals` reverted to bare `dict`. Matches spec exactly.
- [SHOULD-FIX #2 — RESOLVED] `ReplayRun.failures` reverted to `list[dict]`. Matches spec exactly.
- [NIT #3 — RESOLVED] `SensitiveField.retention_policy_id` promoted to first positional field (no default). Dataclass field ordering is valid: sole required field precedes all defaulted fields — no Python error.
- [NIT #4 — RESOLVED] `__all__` omits `ReasonCode` and the module docstring now explicitly states "ReasonCode is defined in companion_harness.reason_codes; import it from there." The inline import comment also reads "intentionally not re-exported; import from reason_codes". Fully addressed.

**New findings:** none.

**Scope check:** Commit touches only `companion_harness/schemas.py` and `companion_harness/reason_codes.py`. No new files, no test modifications, no scope creep.

**Invariant checks:** unchanged from round 1 — all PASS.

**Cross-PR watch-items updated:**
1. RESOLVED — `DecisionTrace.counterfactuals` is now bare `dict`; no future PR needs to defend the narrowing.
2. RESOLVED — `ReplayRun.failures` is now `list[dict]`; no future PR needs to defend the narrowing.
3. Still active — `SensitiveField.retention_policy_id` is now required (positional), which closes the silent-empty-string hazard at the schema level. Watch-item 3 is RESOLVED at schema level. Any PR constructing `SensitiveField` must supply a non-empty `retention_policy_id`; reviewers should spot-check call sites.

---

## PR #2 — Implement event_logger.py (ROADMAP Task 2)  (reviewed 2026-05-13)
**ROADMAP task:** 2
**Verdict:** REQUEST-CHANGES

**Findings:**

- [BLOCKER] `companion_harness/event_logger.py:43–47` — The `log_drop_or_degrade` emission is broken. When `put_nowait(event)` raises `QueueFull`, the queue is still full at that instant; the immediately-following `put_nowait(degrade)` hits the same full queue and falls to the bare `pass` branch. Result: events are silently lost with no `log_drop_or_degrade` record — a direct violation of invariant #10 ("never silently lose events"). Verified empirically: with `maxsize=4` and 20 burst events, zero degrade events appear in the sink. Fix: maintain a separate in-memory degrade counter (or a small overflow buffer, e.g. `collections.deque(maxlen=N)`) and drain that separately, or unconditionally overwrite the oldest queue slot with the degrade event (requires a different queue discipline). The simplest correct approach: keep a `threading.atomic`-style counter and flush degrade events on the next available `put_nowait` (i.e., inside `_drain` after `task_done`).

- [BLOCKER] `tests/test_event_logger.py:65–67` — The test computes `degrade_count` and immediately assigns it to `_`, asserting nothing. The test does not verify that `log_drop_or_degrade` events are actually emitted. Because the degrade path is broken (see above), the test would pass on the broken implementation. Add: `assert degrade_count > 0, "expected degrade events when burst >> queue capacity"`. This is the only assertion that validates the non-silent-loss invariant; without it the test does not meet the ROADMAP Task 2 success criterion ("a synthetic high-rate event stream does not block on a slow sink" proves non-blocking, but nothing proves non-silent-loss).

- [BLOCKER] `tests/test_event_logger.py:62` — `assert len(received) >= 0` is vacuously true (list length is always non-negative). This assertion tests nothing. Delete it.

- [SHOULD-FIX] `companion_harness/event_logger.py:50` — `asyncio.get_event_loop().create_task(...)` inside an `async` method. Since `start()` is itself a coroutine, `asyncio.get_running_loop()` is the correct idiom (introduced Python 3.7, explicit, raises `RuntimeError` if no running loop rather than silently creating one). Replace with `asyncio.get_running_loop().create_task(self._drain())`.

- [SHOULD-FIX] No dependency manifest (`pyproject.toml` / `requirements.txt` / `setup.cfg`) exists. `pytest-asyncio` was installed with `--break-system-packages`, so a fresh checkout has no reproducible install path. This PR introduces the first test that requires a non-stdlib dependency. The PR should either (a) add a minimal `requirements-dev.txt` (one line: `pytest-asyncio>=0.23`) or (b) explicitly defer this to a CI/infra task and add a `README` note — but that deferred task must be created and tracked, since any new developer today cannot run the tests. Leaning toward (a): two lines is not scope creep; the alternative is an unbuildable test suite.

- [NIT] `companion_harness/event_logger.py:13` — `from typing import Any` is imported but never used anywhere in the file. Delete it.

- [NIT] `companion_harness/event_logger.py:78` — `seq_no=-1` in the degrade event breaks the invariant documented in the schema comment ("monotonic within session; catches ordering bugs"). The spec does not prescribe a sentinel value for degrade events. Use `self._seq` (the degrade counter) or a large reserved range. Either is defensible; `-1` is not.

**Scope check:** PR touches only `companion_harness/event_logger.py` and `tests/test_event_logger.py`. All other module stubs and test stubs were present from the initial commit. Scope is clean; no bleed into other tasks.

**Invariant #10 compliance:**
- Realtime path non-blocking (`put_nowait`): PASS — the `log()` method itself is synchronous and never awaits.
- Backpressure emits `log_drop_or_degrade`: FAIL — degrade event is silently lost when queue is full (see BLOCKER above).
- Never silently lose events: FAIL — corollary of above; the `pass` branch is a silent loss path.

**Cross-PR watch-items created:**
4. Once Task 2 is fixed, Task 3 (`causal_graph.py`) must handle `log_drop_or_degrade` events in the DAG: they have `caused_by=[dropped.event_id]`, so the dropped event's ID appears as a cause edge even though the dropped event itself was never delivered to the sink. The orphan detector must not flag the dropped event's ID as an unresolvable cause — or the degrade event's shape must be adjusted so the DAG closes cleanly.

**Cross-PR watch-items checked:**
- Watch-item 3 (SensitiveField.retention_policy_id must be non-empty at call sites): The degrade event in `_make_degrade_event` sets `retention_policy_id="default"`. "default" is non-empty. PASS for this call site.

---

## PR #2 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 2
**Verdict:** APPROVE-WITH-NITS

**Fixes verified (commit 7d770c1):**

- [BLOCKER 1 — RESOLVED] `_dropped` counter approach is correct. `log()` increments `_dropped` atomically (single-threaded asyncio; no race). `_drain` flushes one degrade event directly via `await self._sink(...)`, bypassing the queue entirely. Realtime path never blocks. Degrade records are guaranteed to reach the sink as long as the sink is alive and the drain task has not crashed.

- [BLOCKER 2 — RESOLVED] `assert degrade_count > 0, "expected degrade events when burst >> queue capacity"` is present at test line 64.

- [BLOCKER 3 — RESOLVED] Vacuous `assert len(received) >= 0` is absent from the new test file. Confirmed deleted.

- [SHOULD-FIX 4 — RESOLVED] `asyncio.get_running_loop().create_task(self._drain())` at `start()`. Correct.

- [SHOULD-FIX 5 — RESOLVED] `requirements-dev.txt` added with `pytest>=7.0` and `pytest-asyncio>=0.23`.

- [NIT 6 — RESOLVED] `from typing import Any` import is gone. Confirmed.

- [NIT 7 — RESOLVED] `seq_no=self._seq` (incremented inside `_make_degrade_event`). Degrade events now use a monotonic per-degrade counter. Acceptable — degrade seq_no is in its own namespace separate from caller event seq_nos.

**New findings:**

- [CONCERN] `companion_harness/event_logger.py:_drain` — The direct `await self._sink(degrade)` call at the end of the drain loop has no exception handling. If the sink raises on a degrade event, the exception propagates out of `_drain`, the `asyncio.Task` transitions to done-with-exception, and all subsequent events are silently unlogged forever (the queue keeps filling; `stop()` will hang on `_queue.join()` because `task_done()` is never called for queued events after the crash). The regular-event sink call is guarded by `try/finally` which guarantees `task_done()` even on sink failure, but the degrade call sits outside that guard. Minimum fix: wrap the degrade sink call in a `try/except Exception: pass` or fold it into the same `try/finally` block. This is not a blocker because sink failures are not a defined contract scenario for v0.1a, but it is a latent correctness trap.

- [NIT] `companion_harness/event_logger.py:_make_degrade_event` — `caused_by=[]` loses all causal linkage. The old implementation used `caused_by=[dropped.event_id]`. The new batching design (one degrade per drain cycle, not per dropped event) makes per-event caused_by impractical, but the empty list means the causal graph has a root node that traces to nothing. This is architecturally weaker than a sentinel like `caused_by=["_dropped_before_enqueue"]` but is not a violation of any hard invariant today. Flag for Task 3.

**Scope check:** Commit 7d770c1 touches only `companion_harness/event_logger.py`, `tests/test_event_logger.py`, and `requirements-dev.txt`. No scope creep.

**Invariant #10 compliance:**
- Realtime path non-blocking: PASS — `log()` is synchronous, never awaits.
- Backpressure emits `log_drop_or_degrade`: PASS — counter + direct-sink flush in `_drain` guarantees degrade reaches sink whenever drain is alive.
- Never silently lose events: PASS — with the caveat that a sink crash on the degrade call can kill `_drain` (see CONCERN above).

**Cross-PR watch-items updated:**
- Watch-item 4 UPDATED: The degrade event now has `caused_by=[]` (not `caused_by=[dropped.event_id]` as previously designed). The DAG orphan concern is now simpler — there are no dangling cause edges pointing to dropped events. However Task 3 must treat degrade events as legitimate root nodes (no `caused_by` required). Update orphan detector to whitelist `event_type == "log_drop_or_degrade"` as an allowed root. Watch-item 4 remains active.
- Watch-item 3: `retention_policy_id="default"` on degrade events. Still non-empty. PASS.

---

## PR #2 — re-review round 3  (reviewed 2026-05-13)
**ROADMAP task:** 2
**Verdict:** APPROVE — PR #2 CONVERGED

**Fixes verified (round 2 open items):**

- [CONCERN — RESOLVED] `companion_harness/event_logger.py:_drain` — The degrade sink call is now wrapped in `try/except Exception: pass`. A failing degrade delivery cannot kill the drain loop or cause `stop()` to hang. Confirmed at diff lines in the `_drain` method: `try: await self._sink(...) except Exception: pass`.

- [NIT — RESOLVED] `companion_harness/event_logger.py:_make_degrade_event` — `caused_by=["_dropped_before_enqueue"]` is present. Sentinel string is set; causal linkage is explicit without creating dangling DAG edges.

**New findings:** none.

**Holistic pass — all prior findings status:**
- All 3 round-1 blockers: RESOLVED.
- All round-1 should-fixes and nits: RESOLVED.
- Round-2 CONCERN (degrade sink unguarded): RESOLVED.
- Round-2 NIT (caused_by=[]): RESOLVED.
- No new issues introduced by the round-3 changes. The `try/except Exception: pass` guard is the minimum correct fix; it does not swallow anything that should propagate. The `_dropped_before_enqueue` sentinel is clean and does not create orphan edges.

**Invariant #10 compliance — final:**
- Realtime path non-blocking: PASS.
- Backpressure emits `log_drop_or_degrade`: PASS.
- Never silently lose events: PASS.
- Drain loop robust to sink failures: PASS.
- `stop()` cannot hang due to drain crash: PASS.

**Scope check:** Round-3 changes touch only `companion_harness/event_logger.py`. No scope creep.

**Cross-PR watch-items — final state:**
1. RESOLVED (PR #1 round 2).
2. RESOLVED (PR #1 round 2).
3. RESOLVED at schema level (PR #1 round 2); call-site spot-check ongoing for any PR constructing `SensitiveField`.
4. ACTIVE — Task 3 (`causal_graph.py`) must whitelist `event_type == "log_drop_or_degrade"` as a legitimate DAG root (no `caused_by` required from prior events) AND must handle the `"_dropped_before_enqueue"` sentinel in `caused_by` without flagging it as an unresolvable cause edge. The orphan detector must not reject degrade events.

---

## PR #3 — Implement causal_graph.py (ROADMAP Task 3)  (reviewed 2026-05-13)
**ROADMAP task:** 3
**Verdict:** APPROVE-WITH-NITS

**Findings:**

- [CONCERN] `companion_harness/causal_graph.py:36` — Duplicate `event_id` values are silently resolved by dict overwrite (`self._by_id = {e.event_id: e for e in events}`). If two events share an `event_id`, the first is invisibly dropped from the graph; any event that caused_by-references the first's ID resolves to the second — masking what should be a detectable data-integrity violation. This won't fire in tests today because no fixture generates duplicate IDs, but when the full session log pipeline is live it is plausible (e.g., a retried event_logger flush). Minimum fix: raise `ValueError` on duplicate `event_id` at construction time, or at least add it to `OrphanReport` as a separate `duplicate_ids: list[str]` field.

- [NIT] `companion_harness/causal_graph.py:19` — `_ROOT_EVENT_TYPES` comment says "legitimate DAG roots even when caused_by is non-empty" but the skip at line 48 is unconditional — it also skips `log_drop_or_degrade` events with `caused_by=[]`. The comment is accurate in the sense that non-empty is the *interesting* case, but a reader might wonder whether an empty-caused_by degrade event is also skipped. The behavior is correct; the comment just doesn't say "regardless of caused_by contents". Minor clarity issue, not a behavior bug.

- [NIT] `tests/test_causal_graph_completeness.py:85` — `test_empty_trace_has_no_orphans` has no docstring. Every other test in the file has one. Inconsistent.

**Scope check:** PR touches exactly `companion_harness/causal_graph.py` and `tests/test_causal_graph_completeness.py`. Matches Task 3 exactly. No bleed into other modules.

**Spec fidelity:**
- Orphan definition matches Part 6 Stage 0: events with unresolvable caused_by refs (not in graph, not a known sentinel) are flagged. An empty caused_by is a declared root, not an orphan. PASS.
- Uses the existing `Event` dataclass from `companion_harness.schemas` — not redefined. PASS.
- No speculative helpers, no redundant abstractions, no try/catch, no belt-and-suspenders null-checks. Minimum implementation. PASS.

**Watch-item 4 — RESOLVED:**

Both required conditions verified by code path trace:

(a) `event_type == "log_drop_or_degrade"` treated as valid root: `causal_graph.py` line 48 — `if event.event_type in _ROOT_EVENT_TYPES: continue` — the event is unconditionally skipped regardless of caused_by contents. It will never appear in `orphan_ids`. PASS. Covered by `test_log_drop_or_degrade_is_not_an_orphan`.

(b) `"_dropped_before_enqueue"` sentinel not reported as dangling: `_KNOWN_SENTINELS = frozenset({"_dropped_before_enqueue"})` at line 17; the unresolvable-ref filter at lines 55–57 excludes any ref in `_KNOWN_SENTINELS`. PASS. Covered by `test_sentinel_in_non_root_event_type_is_not_an_orphan` (sentinel on a non-root event type).

Watch-item 4 is fully satisfied and can be closed.

**Cross-PR watch-items created:**
5. `CausalGraph.__init__` silently drops one of any two events sharing the same `event_id` (dict-overwrite). When the full session log pipeline is live, this can mask data-integrity violations as resolved edges. Task 16 (`test_causal_graph_completeness` on real traces) must either (a) confirm the EventLogger guarantees globally unique `event_id` so the scenario is impossible, or (b) add duplicate-ID detection to `CausalGraph` before that test ships.

**Cross-PR watch-items checked:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): `_evt()` fixture helper in tests uses `retention_policy_id="default"`. Non-empty. PASS.
- Watch-item 4: RESOLVED — see above.

---

## PR #3 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 3
**Verdict:** APPROVE — PR #3 CONVERGED

**Fixes verified:**

- [CONCERN — RESOLVED] `companion_harness/causal_graph.py:__init__` — duplicate `event_id` now raises `ValueError` at construction time. Implementation is a single-pass `seen: set[str]` loop before `self._by_id` is built, so the error fires before any inconsistent state is established. Error message includes the offending ID via f-string (`f"duplicate event_id: {e.event_id!r}"`). Correct and minimum.

- [NIT — RESOLVED] `_ROOT_EVENT_TYPES` comment now reads "regardless of caused_by contents." — exactly the requested clarification. No behavior change.

- [NIT — RESOLVED] `test_empty_trace_has_no_orphans` has docstring `"""An empty event list produces an OrphanReport with zero orphans."""` — matches the style of all other tests in the file.

**New test verified:**

`test_duplicate_event_id_raises` constructs `CausalGraph([evt, evt])` inside `pytest.raises(ValueError, match="evt-1")`. The `match` pattern confirms the error message contains the offending ID. `import pytest` was already present at line 9 of the test file (unchanged by this PR). The test actually asserts the raise; it is not vacuous.

**New findings:** none.

**Scope check:** Diff touches only `companion_harness/causal_graph.py` and `tests/test_causal_graph_completeness.py`. No scope creep.

**Holistic pass — all prior findings status:**
- Round-1 CONCERN (silent dict-overwrite on duplicate IDs): RESOLVED.
- Round-1 NIT (comment ambiguity): RESOLVED.
- Round-1 NIT (missing docstring): RESOLVED.
- No new issues introduced.

**Watch-item 4 — still satisfied:** `find_orphans` logic is structurally unchanged from round 1. `_ROOT_EVENT_TYPES` skip and `_KNOWN_SENTINELS` filter intact. PASS.

**Cross-PR watch-items — final state:**
1. RESOLVED (PR #1 round 2).
2. RESOLVED (PR #1 round 2).
3. RESOLVED at schema level (PR #1 round 2); call-site spot-check ongoing.
4. RESOLVED (PR #3 round 1).
5. RESOLVED at harness level — `CausalGraph.__init__` now raises `ValueError` on duplicate `event_id`, closing the data-integrity gap. Task 16 retains a softer obligation: confirm EventLogger does not generate duplicate IDs in practice so that a live session never hits the `ValueError`. That is an EventLogger contract concern, not a `CausalGraph` gap. Watch-item 5 requires no further `CausalGraph` changes.

---

## PR #4 — Implement AudioOutputController (ROADMAP Task 4)  (reviewed 2026-05-13)
**ROADMAP task:** 4
**Verdict:** REQUEST-CHANGES

**Findings:**

- [BLOCKER] `tests/test_audio_output_controller.py:78` — `assert len(audio_sink_calls) < len(chunks) or True` is vacuously true. The `or True` operand makes this assertion a no-op regardless of how many chunks were delivered; the test cannot detect a broken stop path. Delete the `or True` and replace the intent: the stop event fires before `play()` is called in this test (request_stop happens before play()), so `audio_sink_calls` will always be empty — assert `assert audio_sink_calls == []` explicitly. This is the same category as PR #2's vacuous `assert len(received) >= 0` finding.

- [BLOCKER] `companion_harness/audio_output_controller.py:_generation_task` — `self._generation_task` is initialized to `None` in `__init__` and is never assigned anywhere else in the class. `cancel_generation()` checks `if self._generation_task is not None` but that branch can never be true. The `asyncio.Task` cancel path is dead code that gives a false sense of cancellation coverage. Either (a) remove the field and the dead branch entirely, or (b) expose a `set_generation_task(task)` method and document the call contract. Shipping dead cancellation logic under a method named `cancel_generation()` is a correctness hazard — future callers will assume the task is actually cancelled.

- [SHOULD-FIX] `companion_harness/audio_output_controller.py` — `flush()` is a public method that emits `assistant_audio_buffer_flushed` and sets `_playing = False`. `play()` also calls `self.flush(...)` internally at stream completion. The class docstring lifecycle comment reads `start_generation() → queue_buffer() × N → flush() → [stop_requested() if barged]`, implying the caller should call `flush()` as a separate step. But `play()` also calls `flush()` internally — so any caller that invokes both `play()` and `flush()` directly will emit `assistant_audio_buffer_flushed` twice. The two entry-points are not separated clearly: `flush()` should either be private (rename `_flush`) or the lifecycle docstring must be explicit that callers choose `play()` OR explicit `flush()`, never both.

- [SHOULD-FIX] `companion_harness/audio_output_controller.py:_current_generation_event_id` — `self._current_generation_event_id` is assigned in `start_generation()` and never read anywhere in the class. It is dead state. Delete the field and its assignment.

- [NIT] `companion_harness/audio_output_controller.py:play()` — The stop-check fires only at the top of each iteration, before the first chunk. If `request_stop()` is called between plays (i.e., stop event is set, then `start_generation()` is called, then `play()` is called with a subsequent second utterance), `_stop_event` is cleared by `start_generation()`, so stale state is handled correctly. However if `play()` is called without a preceding `start_generation()` (nothing in the API contract prevents this), a stale set stop event from a prior barge-in will cause `play()` to emit `assistant_audio_stop_completed` immediately and deliver zero chunks. The public API needs either a guard or a clear contract comment that `start_generation()` must always precede `play()`.

- [NIT] `companion_harness/audio_output_controller.py:module docstring` — The module-level docstring lists `log_drop_or_degrade` as an event type this adapter emits. The controller never emits `log_drop_or_degrade` — that is emitted by `EventLogger` under backpressure. Remove it from the list to avoid misleading future readers about event ownership.

**Spec fidelity — event names (CRITICAL check):**
All six event_type strings emitted by this controller match the canonical Part 5 names exactly:
- `assistant_generation_start` — PASS
- `assistant_generation_cancel_requested` — PASS
- `assistant_audio_buffer_queued` — PASS
- `assistant_audio_buffer_flushed` — PASS
- `assistant_audio_stop_requested` — PASS
- `assistant_audio_stop_completed` — PASS
No invented or misnamed event types. PASS.

**Invariant #1 (no unlogged behavior):**
- Every public state-transition method emits an Event. PASS.
- `caused_by[]` is required by callers on every method — no code path emits an event with a hardcoded empty list except `play()`'s `assistant_audio_stop_completed` which uses `[generation_event_id]` (non-empty). PASS.
- The non-empty caused_by assertion in the test is correct and covers the full emitted set. PASS.

**Invariant #10 (EventLogger non-blocking):**
- All `_emit()` calls are synchronous and invoke `self._logger.log(evt)` which is the non-blocking synchronous `log()` method established in Task 2. PASS.
- `play()` is async because it awaits the sink, not because it awaits the logger. PASS.

**Adapter purity:**
- No model SDK or audio hardware library imported. `AudioSink` is an injected async callable. PASS.
- `companion_harness/` adapts from interface, not from SDK. PASS.

**Non-blocking stop path:**
- `request_stop()` is a synchronous method: emits event, calls `self._stop_event.set()`, returns. No await, no lock, no queue. PASS.
- The test asserts `elapsed_ms < 50` for the synchronous call. PASS.

**Scope check:**
PR touches only `companion_harness/audio_output_controller.py` and `tests/test_audio_output_controller.py`. Matches Task 4 exactly.

**Cross-PR watch-items created:**
6. `AudioOutputController.cancel_generation()` contains a dead `_generation_task` cancel branch that never fires (no code assigns the field). When Task 5 (VADDetector) or Task 9 (test_thinking_pause) wires async generation tasks into the controller, the caller must either (a) assign to `_generation_task` before calling `cancel_generation()`, or (b) this dead branch is removed and a different cancellation contract is established. Reviewers of Task 5+ must verify this is not left permanently dead.
7. `flush()` / `play()` dual-emission hazard: once Task 5 or Task 9 exercises the full wiring, verify that no caller invokes both `play()` and `flush()` on the same utterance, which would double-emit `assistant_audio_buffer_flushed`. This is a cross-PR correctness trap, not a bug in isolation.

**Cross-PR watch-items checked:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty at call sites): `_emit()` hardcodes `retention_policy_id="default"`. Non-empty. PASS.
- Watch-item 5: EventLogger unique event_id — `event_id = f"{self._session_id}-aoc-{seq}-{now_ms}"` where `seq` is monotonic per controller instance. Unique within a session assuming one controller instance per session. No cross-session collision concern given session_id prefix. PASS for this call site.

---

## PR #4 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 4
**Verdict:** APPROVE — PR #4 CONVERGED

**Fixes verified:**

- [BLOCKER 1 — RESOLVED] `tests/test_audio_output_controller.py:79` — `assert audio_sink_calls == []` is present. Vacuous `or True` is gone. Since `request_stop()` fires before `play()`, the stop_event is set on entry to the loop; the sink is never called. The assertion is both correct and non-vacuous.

- [BLOCKER 2 — RESOLVED] `set_generation_task(task: asyncio.Task[None])` added as a public method. `cancel_generation()` now checks `if self._generation_task is not None and not self._generation_task.done()`, calls `.cancel()`, and clears the field. The cancel branch is live and reachable. New test `test_set_generation_task_cancel` (lines 153–187): creates a real 60-second task, registers it via `set_generation_task`, calls `cancel_generation`, then asserts `task.cancelled() or task.cancelling() > 0`. The `task.cancelling() > 0` arm correctly handles the case where the event loop has not yet run the cancellation; the assertion is not vacuous — it will fail on any implementation that omits the `.cancel()` call. PASS.

- [SHOULD-FIX 3 — RESOLVED] `flush` renamed to `_flush`. Lifecycle docstring updated to `start_generation() → queue_buffer() × N → play() → [stop_requested() if barged]` — `flush()` no longer appears as a public step. No external caller can inadvertently double-emit `assistant_audio_buffer_flushed`.

- [SHOULD-FIX 4 — RESOLVED] `_current_generation_event_id` is absent from `__init__` and from the class body. Dead field deleted.

- [NIT 5 — RESOLVED] Module docstring no longer lists `log_drop_or_degrade`. Confirmed absent.

**Round-1 NIT (play() called without start_generation() — stale stop_event):**
This was a NIT, not a blocker. The coder did not add a guard or contract comment. Acceptable at this severity; no new finding.

**New findings:** none.

**Holistic pass — all prior findings status:**
- Round-1 BLOCKER 1 (vacuous assertion): RESOLVED.
- Round-1 BLOCKER 2 (dead cancel branch): RESOLVED.
- Round-1 SHOULD-FIX 3 (flush public/private ambiguity): RESOLVED.
- Round-1 SHOULD-FIX 4 (dead _current_generation_event_id): RESOLVED.
- Round-1 NIT 5 (log_drop_or_degrade in docstring): RESOLVED.
- Round-1 NIT (play() contract comment): Not addressed; remains a low-severity latent issue for future callers. No new finding created.
- No new issues introduced by round-2 changes.

**Spec fidelity — event names:** All six canonical Part 5 event_type strings present and unchanged. PASS.

**Invariant #1 (no unlogged behavior):** Every public state-transition method emits an Event with non-empty caused_by. PASS.

**Invariant #10 (EventLogger non-blocking):** All `_emit()` calls are synchronous; `play()` is async for the sink only. PASS.

**Adapter purity:** No model SDK or hardware library imported. AudioSink is injected. PASS.

**Scope check:** Diff touches only `companion_harness/audio_output_controller.py` and `tests/test_audio_output_controller.py`. No scope creep.

**Cross-PR watch-items updated:**
- Watch-item 6 RESOLVED — `set_generation_task` makes the cancel branch genuinely live. Any Task 5+ caller that wires generation tasks will call `set_generation_task(task)` before `cancel_generation()`. The dead-branch hazard is closed.
- Watch-item 7 RESOLVED — `_flush` is private (leading underscore convention). External callers cannot invoke it; the `play()` + `flush()` dual-emission path is structurally impossible for callers outside the class. Hazard closed.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
4. RESOLVED (PR #3 round 1).
5. RESOLVED (PR #3 round 2).
6. RESOLVED (this review).
7. RESOLVED (this review).

---

## PR #5 — Wire VADDetector (ROADMAP Task 5)  (reviewed 2026-05-13)
**ROADMAP task:** 5
**Verdict:** APPROVE-WITH-NITS

**Findings:**

- [NIT] `tests/test_vad_detector.py:7` — `import asyncio` is present but never referenced anywhere in the test file. The `@pytest.mark.asyncio` decorator and `await` expressions do not require a direct `asyncio` import by the test author. Delete the import.

- [CONCERN] `companion_harness/turn_detector_vad.py:24-25` — Comment claims these are "Silero VAD v5 recommended defaults for 16 kHz audio" but `docs/implementation-config.yaml` contains no numeric thresholds for the VAD at all — so these values are hardcoded opinions, not config-driven. This is not wrong today (the config file is silent on the values), but it means changing the threshold requires a code edit, not a config edit. When the config file gains numeric VAD fields (as the adapter-config discipline implies it eventually should), these constants must be replaced with a config read. For now: the comment should be changed from "Silero VAD v5 recommended defaults" to something like "module-level defaults; override via constructor args" to avoid implying they trace to a config document that doesn't actually contain them.

**Scope check:** PR touches exactly `companion_harness/turn_detector_vad.py` and `tests/test_vad_detector.py`. Matches Task 5 exactly. No scope creep.

**Adapter purity (CRITICAL) — PASS:**
- `turn_detector_vad.py` imports only stdlib (`hashlib`, `time`, `datetime`, `typing`) plus `companion_harness.*`. No `torch`, no `silero`, no CUDA references anywhere in the module.
- `VADModel` is a `typing.Protocol` — the real Silero implementation is explicitly NOT in this PR.
- Tests inject `_ScriptedVADModel`, a pure-Python fake. No model SDK or GPU dependency reachable from the test.

**Config fidelity:**
- `docs/implementation-config.yaml` specifies `Silero VAD` as the model name but contains NO numeric thresholds. The PR cannot "read from config" because the config has nothing to read. Hardcoded constants (`0.5`, `300`) are exposed as constructor keyword arguments with defaults — callers can override. Not a violation; the config does not prescribe these values.

**TurnSignal spec fidelity (PASS):**
- Uses the existing `TurnSignal` dataclass from `companion_harness.schemas`. Not redefined.
- All six fields present and correctly typed: `detector="vad"`, `p_done`, `p_continue`, `p_backchannel=0.0`, `confidence`, `evidence_event_ids=[frame_evt.event_id]`.

**Invariant #1 (no unlogged behavior) — PASS:**
- Every call to `process_frame()` emits a `vad_frame` event via `EventLogger`.
- EOU events emit a second `vad_turn_signal` event.
- `caused_by` on all events is caller-supplied (not empty by construction). Test asserts `evt.caused_by` is truthy for every received event.
- `vad_frame` events use the caller's `caused_by`; `vad_turn_signal` uses `[frame_evt.event_id]` — causal chain closes.

**Test quality — PASS:**
- `test_detector_emits_turn_signal_on_speech_then_silence`: scripted probs (10×0.9 speech, 10×0.1 silence), asserts `len(signals) == 1`, checks all TurnSignal fields including `evidence_event_ids` length. Non-vacuous throughout.
- Math is correct: 10 × 32ms = 320ms accumulated silence >= 300ms threshold; signal fires on the 10th silence frame.
- `test_detector_does_not_emit_during_continuous_speech`: asserts `all(s is None ...)` — not vacuous.
- `test_no_torch_import`: source-text check for `import torch` and `from torch`. Correct and sufficient.
- No `or True`, no `>= 0`, no discarded results.

**Watch-item 3 (SensitiveField.retention_policy_id non-empty):**
- `VADDetector` constructs no `SensitiveField` instances. Events use `retention_policy_id="default"` (non-empty). PASS for this PR.

**Cross-PR watch-items created:**
8. `_SPEECH_THRESHOLD` and `_SILENCE_ONSET_MS` are hardcoded module constants that the comment misleadingly implies trace to Silero v5 documentation. When `docs/implementation-config.yaml` gains numeric VAD fields, these constants must become config reads rather than code literals. Flag when reviewing the config-wiring task (no current ROADMAP task assigned).

**Cross-PR watch-items checked:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): no SensitiveField constructed in this PR; `retention_policy_id="default"` on all events. PASS.
- Watch-item 6 (AudioOutputController.cancel_generation dead-branch): not touched by this PR. No wiring of generation tasks into the controller here. Still closed (RESOLVED in PR #4 round 2).
- Watch-item 7: RESOLVED in PR #4 round 2; not relevant here.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (New) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) are hardcoded constants, not config reads. Must become config reads when `implementation-config.yaml` gains VAD numeric fields.

---

## PR #5 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 5
**Verdict:** APPROVE — PR #5 CONVERGED

**Fixes verified (commit 6ef1131):**

- [NIT — RESOLVED] `tests/test_vad_detector.py` — `import asyncio` is absent. Confirmed: the file opens with `import pytest` followed directly by `from companion_harness.*` imports. No asyncio import present.

- [CONCERN — RESOLVED] `companion_harness/turn_detector_vad.py:24-25` — Comment no longer claims Silero v5 provenance. New text reads: `# Module-level defaults; override via constructor keyword args.` followed by `# (implementation-config.yaml names the Silero model but does not yet specify numeric VAD thresholds.)` This is accurate: `implementation-config.yaml` lists `Silero VAD` under `TurnDetectorSuite.VADDetector` but contains zero numeric fields of any kind. The comment is truthful; no false provenance claim remains.

**New findings:** none.

**Holistic pass — all prior round-1 PASS items:**
- Adapter purity (no torch, VADModel as Protocol): PASS. Unchanged.
- TurnSignal fidelity (all six fields, correct types): PASS. Unchanged.
- Invariant #1 (every frame logged, caused_by closes): PASS. Unchanged.
- Test quality (no vacuous assertions, math correct, torch-import check): PASS. Unchanged.
- Scope (touches only turn_detector_vad.py and test_vad_detector.py): PASS. Unchanged.

**Scope check:** Round-2 commit touches only `companion_harness/turn_detector_vad.py` (comment reword, 2 lines changed) and `tests/test_vad_detector.py` (1 line removed). No scope creep.

**Watch-item 8 — status:** Still ACTIVE. The reworded comment makes the situation honest (no false config provenance) but does not wire the constants to config. Watch-item 8 remains open for the future config-wiring task. The comment's parenthetical now accurately describes the gap, so the hazard is flagged in the code itself.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) are hardcoded module constants. Must become config reads when `implementation-config.yaml` gains numeric VAD fields. The in-code comment now accurately states this gap; no code change required until the config gains those fields.

---

## PR #6 — Implement SpeakPolicy (ROADMAP Task 6)  (reviewed 2026-05-13)
**ROADMAP task:** 6
**Verdict:** REQUEST-CHANGES

**Test file check:**
ROADMAP Task 6 success criterion reads: "Verify: deterministic given recorded signals (Tier B replay precondition)." It does NOT name a pytest target (unlike Task 2's "a synthetic high-rate event stream does not block" or Task 3's "orphan detector unit test passes"). There is no pre-existing `pytest.skip` stub for a `test_speak_policy` file. However, CLAUDE.md rule 4 requires each PR to turn a `pytest.skip` into a passing test, ship a stub, or be a docs update. This PR is a full behavior implementation that turns no skip green and is not a stub. This is a rule 4 violation — see BLOCKER below. The manual test in the PR body is not a substitute for a pytest-tracked test.

**Findings:**

- [BLOCKER] `companion_harness/speak_policy.py:16` — `"quiet_mode"` is not a valid `privacy_mode` value. The spec (Part 7) defines `privacy_mode` as `normal | no_memory | no_camera_memory | local_only | guest_present | child_present | sensitive_conversation`. There is no `quiet_mode` privacy_mode in the spec. This entry in `_BLOCKING_PRIVACY_MODES` will never match any real caller input, producing a silent dead-code path that gives a false sense of quiet-mode blocking. Furthermore, "quiet mode" is a user command (invariant #7), not a privacy_mode label. Fix: remove `"quiet_mode"` from `_BLOCKING_PRIVACY_MODES`. If quiet-mode-as-privacy-mode is intentional, the spec must first define it; a policy implementation is not the place to invent new `privacy_mode` values.

- [BLOCKER] CLAUDE.md rule 4: "Each PR ships exactly one of: (a) a stub for a future capability, (b) an implementation that turns a `pytest.skip` into a passing test, (c) a documentation update." This PR delivers a full `decide()` implementation with no pytest coverage at all — no new test file, no skip converted to green. The manual test in the PR body is not tracked by the CI test runner and provides no regression safety. Required fix: add `tests/test_speak_policy.py` with, at minimum: (1) a full_response case with `eou_probability=0.9` + `user_addressed_agent=True` + neutral modes; (2) a silence/tie case with `eou_probability=0.5`; (3) a privacy-mode block case; (4) a social-mode block case; (5) a determinism assertion (call `decide()` twice with identical inputs, assert `==`). These are not speculative — they directly verify the Task 6 success criterion "deterministic given recorded signals."

- [SHOULD-FIX] `companion_harness/speak_policy.py:50` — When `user_speaking=True`, the returned `primary_reason_code` is `COOLDOWN_BLOCKED`. `COOLDOWN_BLOCKED` semantically means a timer-based cooldown is preventing speech (see the enum: it pairs with `cooldown_state`). Waiting because the user's turn is not finished is not a cooldown; it is the normal EOU gate. There is no perfect enum member for this case, but `COOLDOWN_BLOCKED` is the most misleading available choice and will produce incorrect "why did you say that?" explanations. The spec's enum does not cover "user mid-turn" explicitly — that is a gap. For now, using `NOT_ADDRESSED_TO_AGENT` is more accurate (if the user is speaking, the turn has not been handed off; the agent is not being addressed to respond). Flag this as a ReasonCode enum gap and use the least-wrong member. Do not use `COOLDOWN_BLOCKED` for a non-cooldown condition.

- [SHOULD-FIX] `companion_harness/speak_policy.py:54` — When `eou_probability <= 0.5`, the returned `primary_reason_code` is `COOLDOWN_BLOCKED`. Same problem as above: a probability threshold is not a cooldown. `COOLDOWN_BLOCKED` will mislead "why did you say that?" queries into suggesting a timer was active. Correct to a less-wrong member (e.g. `NOT_ADDRESSED_TO_AGENT` if the EOU gate is conceptually "turn not confirmed") and add a comment explaining the ReasonCode gap.

- [CONCERN] `companion_harness/speak_policy.py:38` — `decide()` accepts `signal_event_ids: list[str]` and constructs `caused_by=list(signal_event_ids)`. If the caller passes an empty list, the returned `SpeakDecision` has `caused_by=[]` — an orphan decision with no causal link. The Stage 0 invariant ("every action traces to either user input or scheduled trigger") and the project rule ("Orphan events fail Stage 0 contract tests. The DAG must close.") apply. The function provides no guard or documented contract that callers must supply non-empty `signal_event_ids`. Minimum fix: add a docstring contract note that `signal_event_ids` must be non-empty and/or raise `ValueError` if called with an empty list.

- [NIT] `companion_harness/speak_policy.py:60` — `budget_bucket="full_response"` is a free string. There is no spec definition of `budget_bucket` values. Using the action_type string as the bucket name is sensible, but should be a named constant or at least an inline comment explaining the convention, so future maintainers don't invent divergent strings.

**Scope check:** Touches only `companion_harness/speak_policy.py`. No bleed. The scope of changes is correct; the gap is the missing test file.

**Invariant #5 (determinism) — PASS:**
- No `time`, `datetime`, `random` imports or calls anywhere in the diff.
- `frozenset` membership tests (`in _BLOCKING_PRIVACY_MODES`, `in _BLOCKING_SOCIAL_MODES`) are O(1) hash lookups — the output is a bool, not a set iteration; no ordering dependence.
- `PolicyInputs` field accesses are direct attribute reads — no dict iteration affecting output.
- `caused_by` is constructed as `list(signal_event_ids)` — deterministic given same inputs.
- Two calls with identical `PolicyInputs` and `signal_event_ids` produce `==` results. PASS.

**v0.1a action-set restriction — STRUCTURAL PASS:**
- Only one code path returns a non-silence `SpeakDecision`: the explicit `action_type="full_response"` constructor at line ~57. All other branches call `_silence()`, which hardcodes `action_type="silence"`. The six forbidden action types (`backchannel`, `short_reaction`, `clarification`, `alert`, `tool_status`, `aesthetic_reaction`) are unreachable not by luck but because no code path constructs them. STRUCTURAL PASS.

**`primary_reason_code` on every decision — CONDITIONAL PASS:**
- Every `SpeakDecision` returned by `decide()` carries a `primary_reason_code` from `ReasonCode`. All six codes used (`QUIET_MODE_BLOCKED`, `NOT_ADDRESSED_TO_AGENT`, `COOLDOWN_BLOCKED`, `EOU_CONFIRMED`, `USER_ADDRESSED_AGENT` as supporting) are real enum members. However, two codes are semantically incorrect for their assigned branches (see SHOULD-FIX above).

**Adapter purity — PASS:**
- Only `companion_harness.reason_codes` and `companion_harness.schemas` imported. No model SDK reachable.

**Invariant #8 (silence wins ties) — CONDITIONAL PASS:**
- `eou_probability <= 0.5` → silence, `> 0.5` with addressing → full_response. The `<=` ensures exact 0.5 resolves to silence. This is a faithful encoding of "silence wins ties" for the EOU probability dimension.
- However, the spec (Part 3 Stage 3) says "Silence wins ties" as a general policy principle, not just a threshold. Other tie conditions (e.g., `user_addressed_agent` ambiguous, `urgency_score` near threshold) are not handled — those are deferred correctly to later stages, since v0.1a only has `{silence, full_response}`. Acceptable for v0.1a scope.

**Watch-item 3 (SensitiveField.retention_policy_id non-empty):**
`speak_policy.py` constructs no `SensitiveField` instances. PASS for this PR.

**Watch-item 8 (VAD numeric defaults not config-driven):**
Not touched by this PR. Still active.

**Cross-PR watch-items created:**
9. `_BLOCKING_PRIVACY_MODES` in `speak_policy.py` contains `"quiet_mode"`, which is not a valid `privacy_mode` spec value. Once the BLOCKER is fixed by removing it, the correct behavioral hook for quiet-mode must be designed (possibly as a separate `quiet_mode` field on `PolicyInputs`, or as a named constant in the companion_state schema). Future PRs that add quiet-mode support must NOT add it as a `privacy_mode` string — it requires a spec-aligned field. Flag when reviewing any PR that touches `companion_state` or adds quiet-mode product behavior.

**Cross-PR watch-items checked:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): no SensitiveField constructed. PASS.
- Watch-item 8 (VAD numeric defaults): not touched. Still active.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) are hardcoded module constants. Must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (New) `quiet_mode` is not a valid `privacy_mode` value per spec Part 7. Any PR adding quiet-mode product behavior must NOT implement it as a `privacy_mode` string — it requires a new spec-aligned field. Track until quiet-mode is spec-defined and correctly implemented.

---

## PR #6 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 6
**Verdict:** APPROVE-WITH-NITS — PR #6 CONVERGED

**Fixes verified:**

- [BLOCKER 1 — RESOLVED] `_BLOCKING_PRIVACY_MODES` is now `frozenset()` (empty). Spec Part 7 lists seven valid `privacy_mode` values; none are designated speech-blocking for v0.1a (they govern logging/memory/adapter behavior only). The comment on the line accurately states this. The conclusion "no privacy mode is speech-blocking for v0.1a" is spec-faithful. The privacy-block branch is now dead code for v0.1a — see NIT below.

- [BLOCKER 2 — RESOLVED] `tests/test_speak_policy.py` ships 5 tests:
  1. `test_full_response` — asserts `action_type == "full_response"` + `primary_reason_code == EOU_CONFIRMED`. Non-vacuous.
  2. `test_silence_at_tie` — `eou_probability=0.5` → silence; directly exercises the `<=` boundary of invariant #8. Non-vacuous.
  3. `test_social_mode_block` — `group_conversation` → silence + `NOT_ADDRESSED_TO_AGENT`. Non-vacuous.
  4. `test_determinism` — two calls with identical `PolicyInputs` and `signal_event_ids`; asserts `d1 == d2`. `SpeakDecision` is a dataclass; equality is value-based. Non-vacuous.
  5. `test_empty_signal_event_ids_raises` — `pytest.raises(ValueError)` with empty list. Non-vacuous.
  
  The test file docstring explains why no privacy-mode block case is included: `_BLOCKING_PRIVACY_MODES` is empty, so that branch is unreachable and a test for it would be vacuous. This is accurate and acceptable.

- [SHOULD-FIX 1 & 2 — RESOLVED] Both `user_speaking=True` and `eou_probability <= 0.5` branches now emit `NOT_ADDRESSED_TO_AGENT`, with inline comments flagging the ReasonCode enum gap. This is the least-wrong available code as required.

- [CONCERN — RESOLVED] `if not signal_event_ids: raise ValueError(...)` is the first statement in `decide()`, before any `SpeakDecision` is constructed. Guard fires immediately; no partial state created.

- [NIT — RESOLVED] `budget_bucket="full_response"` now carries an inline comment: `# budget_bucket selects the per-action latency budget from speak_policy config`. Addressed.

**New findings:**

- [NIT] `companion_harness/speak_policy.py:43` — `if inputs.privacy_mode in _BLOCKING_PRIVACY_MODES:` is dead code for v0.1a: `_BLOCKING_PRIVACY_MODES` is `frozenset()` (empty), so this branch can never be taken. CLAUDE.md rule 2 says "No features beyond what the current ROADMAP task asks for" and "No abstractions for single-use code." A permanently-dead guard is speculative scaffolding. The comment on the frozenset line accurately describes why it is empty, but the branch itself adds no behavior. Minimum fix: delete the `if inputs.privacy_mode in _BLOCKING_PRIVACY_MODES` block and the `_BLOCKING_PRIVACY_MODES` declaration. When a future spec change designates a privacy mode as speech-blocking, that PR can add the set and the guard together as a single coherent change. Not a blocker; callers are unaffected.

**Round-1 PASS items — still intact:**
- Invariant #5 (determinism): no time/random calls; all comparisons are pure field reads. PASS.
- v0.1a action-set restriction: only `silence` and `full_response` reachable by any code path. PASS.
- `primary_reason_code` on every decision: all six branches set it. PASS.
- Adapter purity: only `companion_harness.reason_codes` and `companion_harness.schemas` imported. PASS.
- Invariant #8 (silence wins ties): `eou_probability <= 0.5` → silence; `=` boundary correctly goes to silence. PASS.

**Scope check:** Diff touches only `companion_harness/speak_policy.py` and `tests/test_speak_policy.py`. No scope creep.

**Watch-item 9 — UPDATED:** The immediate hazard (`"quiet_mode"` in `_BLOCKING_PRIVACY_MODES`) is gone. The design guard remains: any future PR adding quiet-mode product behavior must NOT encode it as a `privacy_mode` string. Watch-item 9 narrows to that forward constraint.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) are hardcoded module constants. Must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Updated) `quiet_mode` must NOT be implemented as a `privacy_mode` string in any future PR. The existing bad code has been removed. Track until quiet-mode is spec-defined (with its own field on `PolicyInputs` or `companion_state`) and correctly implemented.

---

## PR #6 — re-review round 3  (reviewed 2026-05-13)
**ROADMAP task:** 6
**Verdict:** APPROVE — PR #6 CONVERGED

**Round-2 NIT addressed (single open item):**

- [NIT — RESOLVED] `companion_harness/speak_policy.py` — `_BLOCKING_PRIVACY_MODES = frozenset()` declaration and the `if inputs.privacy_mode in _BLOCKING_PRIVACY_MODES:` guard branch are both fully absent from the PR branch. No orphan reference, no dangling comment, no import of the name from any other file. Confirmed via `git grep` across `companion_harness/` and `tests/`: zero matches for `BLOCKING_PRIVACY`. The removal is complete.

**Decision logic verification post-removal:**

Every path through `decide()` still terminates correctly:
1. `social_mode in _BLOCKING_SOCIAL_MODES` → `_silence(NOT_ADDRESSED_TO_AGENT)`. PASS.
2. `user_speaking` → `_silence(NOT_ADDRESSED_TO_AGENT)`. PASS.
3. `eou_probability <= 0.5` → `_silence(NOT_ADDRESSED_TO_AGENT)`. PASS.
4. `user_addressed_agent` → `SpeakDecision(action_type="full_response", ...)`. PASS.
5. Fallthrough (EOU confirmed, not addressed) → `_silence(NOT_ADDRESSED_TO_AGENT)`. PASS.

No branch became unreachable; no branch was broken. The removed privacy-mode guard was dead code for v0.1a (the frozenset was already empty in round 2), so its deletion carries zero behavioral change.

**Prior-round PASS items — all intact:**
- Invariant #5 (determinism): no time/random calls; pure field reads. PASS.
- v0.1a action-set restriction: only `silence` and `full_response` constructible. PASS.
- `primary_reason_code` on every decision: all five branches set it. PASS.
- Adapter purity: only `companion_harness.reason_codes` and `companion_harness.schemas` imported. PASS.
- Invariant #8 (silence wins ties): `eou_probability <= 0.5` → silence; exact boundary correctly goes to silence. PASS.
- `ValueError` guard on empty `signal_event_ids`: first statement in `decide()`. PASS.
- Five tests present, all non-vacuous, all passing. PASS.

**Test file reference check:**
The test docstring still references `_BLOCKING_PRIVACY_MODES` by name ("so _BLOCKING_PRIVACY_MODES is empty"). The set no longer exists in `speak_policy.py`, making this a stale reference in a comment. This is a NIT-level inaccuracy (the sentence remains logically true — the concept is gone, which is even better than empty — but it references a name that no longer exists). Not a blocker; the explanation is still comprehensible and the test coverage itself is unaffected.

**New findings:** none (the stale docstring reference noted above does not rise above a nit, and the PR has already converged through two rounds of substantive change — raising it as a new finding that blocks merge would be disproportionate).

**Holistic pass — PR #6 fully satisfies ROADMAP Task 6:**
- `decide()` is implemented and correct.
- Five tests cover all required scenarios named in the round-1 BLOCKER.
- Invariants #5, #8 verified by both code inspection and test.
- No adapter purity violation.
- No scope creep.
- CLAUDE.md rule 4 satisfied: PR ships an implementation that exercises the Task 6 success criterion ("deterministic given recorded signals — Tier B replay precondition").

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) are hardcoded module constants. Must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string in any future PR. Track until quiet-mode is spec-defined with its own field on `PolicyInputs` or `companion_state`.

Watch-items 3, 8, 9 status: unchanged from round 2. None are affected by this PR's changes.

---

## PR #7 — Wire ForegroundModel adapter (ROADMAP Task 7)  (reviewed 2026-05-13)
**ROADMAP task:** 7
**Verdict:** REQUEST-CHANGES

**ROADMAP Task 7 success criterion:** "Wire `ForegroundModel` adapter. One adapter interface, one concrete instantiation behind it. Verify: adapter interface importable from `speak_policy.py` without leaking the SDK."

**Rule 4 / test-coverage analysis:**
ROADMAP Task 7 is NOT a pure stub (option a) — `process_frame()` contains real, functional event-logging + model-call logic. It is NOT a docs update (option c). Under rule 4(b) it must turn a `pytest.skip` into a passing test. No `test_foreground_model.py` stub existed before this PR (confirmed: 26 tests collected, 19 passed / 7 skipped — unchanged baseline). The PR adds no test file and converts no skip. The "manual test" in the PR body (`python -c "from companion_harness.foreground_model import ...`) is not tracked by the CI test runner and provides zero regression safety. This is the identical rule 4 violation flagged as BLOCKER on PR #6. Same standard applies.

**Findings:**

- [BLOCKER] `companion_harness/foreground_model.py` — No test file shipped. `process_frame()` has real logic (event emission, model call, proposal wrapping); this PR is a functional implementation, not a pure stub, and rule 4(b) therefore requires a pytest test that turns a skip green. The Task 7 success criterion ("importable without leaking SDK") is directly testable as a pytest: check that `import companion_harness.foreground_model` succeeds and that `"torch"` is absent from `sys.modules` afterward. Required minimum test file `tests/test_foreground_model.py` must include at least: (1) an import-cleanliness test (no torch/SDK in `sys.modules` after import); (2) a `process_frame` returns-None path test (scripted model returns None, no proposal event emitted); (3) a `process_frame` returns-proposal path test (scripted model returns a `ThinkerProposal`, adapter logs `foreground_frame` + `foreground_proposal` events with correct `caused_by`, returns the proposal); (4) a non-vacuous `caused_by` assertion on all emitted events. These tests need no GPU — a scripted `DuplexModel` fake (plain Python class) is sufficient.

- [BLOCKER] `companion_harness/foreground_model.py:71` — `foreground_frame` and `foreground_proposal` are invented event_type strings. The spec (Part 5) enumerates event_types for `AudioOutputController` and states the list is "not exhaustive — extend as needed," but provides no `foreground_*` names. More critically, the spec (Part 3) maps the ForegroundModel role to `ThinkerProposalGen` ("proposal-only, no direct speech path"), and `docs/implementation-config.yaml` has `ThinkerProposalGen` as a named adapter distinct from `ForegroundModel`. Using `foreground_frame` / `foreground_proposal` as event_type strings is plausible as an extension, but the coder should explicitly call out that these are new names being introduced, confirm they don't conflict with any future spec-named types, and use a consistent naming convention. The ambiguity is whether `ForegroundModel` in this codebase corresponds to the spec's `ForegroundModel` adapter (which processes multimodal input) or the spec's `ThinkerProposalGen` (which emits proposals only) — the docstring conflates both roles. Clarify which adapter this is implementing, and rename event types accordingly (e.g., `thinker_frame` / `thinker_proposal` if it's `ThinkerProposalGen`). This naming ambiguity will confuse Stage 0 causal graph queries. Flag as BLOCKER because it must be decided before the event_type strings become stable references in fixture logs.

- [CONCERN] `companion_harness/foreground_model.py:71–72` — The `foreground_proposal` event is logged but the returned `ThinkerProposal` is the raw object from `model.infer()`. The `ThinkerProposal.caused_by` field (required by spec Part 5 schema) is entirely the injected model's responsibility to populate. If the real MiniCPM-o adapter or a test fake returns a `ThinkerProposal` with `caused_by=[]`, the proposal is a causal orphan — a direct violation of invariant #1. The `ForegroundModel` adapter should either (a) enforce non-empty `caused_by` on the returned proposal (raising `ValueError` or overwriting with `[frame_evt.event_id]`), or (b) document a hard contract that callers/model implementations must populate `ThinkerProposal.caused_by` with at minimum the frame event id. Without this, the DAG invariant is left to the honor system of every future `DuplexModel` implementor.

- [NIT] `companion_harness/foreground_model.py:97` — `payload_kind="model_output"` is assigned to `foreground_frame` events. A raw audio frame fed into the model is not a model output — it is input signal. The spec's valid `payload_kind` values are `"signal" | "transcript" | "raw_audio" | "raw_video" | "model_output" | "memory_op" | "tool_event"`. The `foreground_frame` event should use `payload_kind="signal"` (or `"raw_audio"` if the frame is unprocessed PCM). `"model_output"` belongs on the `foreground_proposal` event, not the frame event. As written, the frame event is misclassified, which will confuse retention policy enforcement (model_output has different retention rules than signal/raw_audio).

**Scope check:**
PR touches exactly one new file (`companion_harness/foreground_model.py`). No other module modified. Scope discipline is correct; the gap is the missing test.

**Adapter purity — PASS:**
- Imports are stdlib only (`hashlib`, `time`, `datetime`, `timezone`) plus `companion_harness.event_logger` and `companion_harness.schemas`. No `torch`, `transformers`, `CUDA`, `MiniCPM`, or any model SDK anywhere in the import chain. PASS.
- `DuplexModel` is a `typing.Protocol` (correctly `@runtime_checkable`). The real SDK-backed implementation is NOT in this PR. PASS.
- `speak_policy.py` is not imported anywhere in `foreground_model.py`. PASS.
- `companion_harness/foreground_model.py` import chain: stdlib + `Event`/`ThinkerProposal` from schemas (stdlib only) + `EventLogger` from event_logger (stdlib only). Import would succeed on a machine with no model SDK. PASS.

**Invariant #2 (no direct Thinker speech) — PASS:**
- `process_frame()` returns `ThinkerProposal | None`, never `SpeakDecision`. PASS.
- No import or call to `speak_policy.py`. PASS.
- No `action_type` decision made anywhere in the file. PASS.

**Invariant #4 (no proactive speech without policy approval) — PASS:**
- `ForegroundModel` never calls `SpeakPolicy.decide()`. PASS.

**Invariant #9 (no invented tool progress) — PASS:**
- No tool progress narration. PASS.

**ThinkerProposal spec fidelity — PASS:**
- Returns the existing `ThinkerProposal` dataclass from `companion_harness.schemas`, not a redefinition. PASS.
- All 9 spec fields present in the dataclass: `proposal_type`, `content`, `trigger`, `confidence`, `novelty`, `interruption_cost`, `max_utterance_ms`, `cooldown_consumed`, `caused_by`. The adapter does not construct the proposal directly (it comes from the injected model), so field population is the model's responsibility. PASS structurally.

**"One interface, one instantiation" — PASS:**
- Single `DuplexModel` Protocol. Single `ForegroundModel` class. No sprawl. PASS.

**Watch-item 3 (SensitiveField.retention_policy_id non-empty):**
- `foreground_model.py` constructs no `SensitiveField` instances. `retention_policy_id="default"` on all events. PASS.

**Watch-items 8, 9:**
- Not touched by this PR. Still active.

**Cross-PR watch-items created:**
10. `foreground_frame` events use `payload_kind="model_output"` but these are input signal events. Once Task 8 (fixtures) or Task 9 (`test_thinking_pause`) uses real replay logs, any retention-policy enforcement that differentiates `model_output` from `signal`/`raw_audio` will misclassify these events. Fix `payload_kind` on frame events before fixture logs are stamped. Flag when reviewing Task 8.
11. `ThinkerProposal.caused_by` is populated by the injected model, not enforced by the adapter. Every future `DuplexModel` implementation (including the b200 MiniCPM-o wrapper) must populate `caused_by` with at least `[frame_event_id]`. Reviewers of the b200 model implementation PR must verify this; an empty `caused_by` on any returned proposal is a DAG invariant violation.

**Cross-PR watch-items checked:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): no SensitiveField constructed. PASS.
- Watch-item 8 (VAD numeric defaults): not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): not touched. Still active.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) are hardcoded module constants. Must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string in any future PR. Track until quiet-mode is spec-defined with its own field on `PolicyInputs` or `companion_state`.
10. (New) `foreground_frame` events carry `payload_kind="model_output"` (wrong — should be `"signal"` or `"raw_audio"`). Fix before fixture logs are stamped in Task 8.
11. (New) `ThinkerProposal.caused_by` is not enforced by the adapter. Every `DuplexModel` implementation must populate it. Verify on the b200 MiniCPM-o wrapper PR.

---

## PR #7 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 7
**Verdict:** APPROVE — PR #7 CONVERGED

**Fix verification — BLOCKER 1 (role/API ambiguity):**

Independent spec check performed. Part 3 lists both `ForegroundModel` and `ThinkerProposalGen` as named adapters in the ASCII table but gives zero API signatures for either. Part 5 defines `ThinkerProposal` (the output type) but no adapter method signatures. Part 9 assigns `MiniCPM-o 4.5 (as_duplex)` to `ForegroundModel` and the Inner Thoughts loop to `ThinkerProposalGen` — distinguishing them by model/mode, not by API shape. The coder's "GENUINE SPEC AMBIGUITY" conclusion is accurate.

Additional supporting evidence the coder did not cite: Part 8 (v0.1a MVP scope) explicitly enables `ForegroundModel` and is silent on `ThinkerProposalGen`, which implicitly lives in Stage 6 (texture, disabled at v0.1a). This makes `ForegroundModel` the only correct choice for ROADMAP Task 7 and further validates the interpretation.

The `# SPEC AMBIGUITY:` block in the module docstring is clear and honest: it states what the spec says, why the distinction is model/mode not API, which adapter this module implements and under which authority (ROADMAP Task 7 + Part 9), and what would trigger a required update. Acceptable to APPROVE. The documented ambiguity is escalated, not hidden.

The `foreground_frame`/`foreground_proposal` event type names are introduced as spec extensions, citing Part 5's "not exhaustive — extend as needed." This is explicit and correctly attributed.

**Fix verification — BLOCKER 2 (tests):**

Four tests present in `tests/test_foreground_model.py`:

1. `test_no_torch_import` — asserts `"torch" not in sys.modules`. Non-vacuous; directly tests Task 7 success criterion.
2. `test_none_path_emits_only_frame_event` — scripted model returns `None`; asserts `result is None` and `event_types == ["foreground_frame"]` (list equality, not `len >= 0`). Non-vacuous.
3. `test_proposal_path_emits_both_events_with_caused_by` — asserts both event types logged; `frame_evt.caused_by == ["input-evt-1"]`; `proposal_evt.caused_by == [frame_evt.event_id]`; `frame_evt.payload_kind == "raw_audio"`; `proposal_evt.payload_kind == "model_output"`. Full causal chain verified. Non-vacuous.
4. `test_empty_caused_by_repaired_to_frame_event_id` — scripted proposal with `caused_by=[]`; asserts repair to `[frame_evt.event_id]` with an f-string error message. Non-vacuous.

All four tests are GPU-free (scripted `_FakeModel` with no SDK dependency). All four test the right behavior.

**Fix verification — CONCERN (caused_by repair):**

`process_frame()` contains `if not proposal.caused_by: proposal.caused_by = [frame_evt.event_id]` before the proposal is returned. Guard fires on `caused_by=[]`. Tested by test 4 above. RESOLVED.

**Fix verification — NIT (payload_kind):**

`foreground_frame` is emitted via `self._emit("foreground_frame", caused_by, "raw_audio")` — explicit `"raw_audio"` arg overrides the `"model_output"` default. `foreground_proposal` uses `"model_output"` (explicit arg). Test 3 asserts both. Watch-item 10 RESOLVED.

**Watch-item 11 — RESOLVED:**

The adapter now enforces `caused_by` repair on any returned proposal (`if not proposal.caused_by: proposal.caused_by = [frame_evt.event_id]`). The DAG closure invariant is guaranteed at the adapter layer regardless of what the injected model returns. Future `DuplexModel` implementations no longer bear sole responsibility for this — the adapter has the backstop. Watch-item 11 is closed; no separate verification required on the b200 wrapper PR (though that PR should still populate `caused_by` correctly as a matter of correctness, since the adapter repairs rather than validates).

**Round-1 PASS items — still intact:**
- Adapter purity: no torch/SDK imports; `DuplexModel` is a Protocol; `speak_policy.py` not imported. PASS.
- Invariant #2 (no direct Thinker speech): `process_frame()` returns `ThinkerProposal | None`, never `SpeakDecision`. PASS.
- Invariant #4 (no proactive speech without policy approval): no `SpeakPolicy.decide()` call. PASS.
- ThinkerProposal fidelity: uses existing `ThinkerProposal` dataclass from schemas; not redefined. PASS.
- One interface, one instantiation: single `DuplexModel` Protocol, single `ForegroundModel` class. PASS.

**New findings:** none.

**Scope check:** PR adds exactly two files: `companion_harness/foreground_model.py` and `tests/test_foreground_model.py`. No other module modified. Clean.

**Watch-item 3 (SensitiveField.retention_policy_id non-empty):**
`foreground_model.py` constructs no `SensitiveField` instances. All events use `retention_policy_id="default"`. PASS.

**Watch-item for ThinkerProposalGen:**
The spec ambiguity between `ForegroundModel` and `ThinkerProposalGen` APIs remains unresolved in the spec. When Stage 6 (texture) work begins and `ThinkerProposalGen` is implemented (ROADMAP task beyond Task 17), the reviewer must:
(a) check whether the spec has been updated to distinguish the two APIs, and if not, decide whether to reuse `process_frame()` or define a separate interface;
(b) confirm that `ThinkerProposalGen` proposals route through `SpeakPolicy` and do not introduce a second speech path.
This is a forward-looking design constraint, not a current blocker. Creating watch-item 12 below.

**Cross-PR watch-items — final state for this PR:**
- Watch-item 10: RESOLVED — `foreground_frame` payload_kind corrected to `"raw_audio"`.
- Watch-item 11: RESOLVED — `caused_by` repair enforced at adapter layer; DAG closure guaranteed.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) are hardcoded module constants. Must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string in any future PR. Track until quiet-mode is spec-defined with its own field on `PolicyInputs` or `companion_state`.
12. (New) When `ThinkerProposalGen` is implemented (Stage 6, post-v0.1a), reviewer must: (a) check whether the spec has differentiated `ForegroundModel` and `ThinkerProposalGen` APIs — if not, decide whether to reuse `process_frame()` or define a new interface; (b) confirm `ThinkerProposalGen` proposals route through `SpeakPolicy` and do not create a second speech path (invariant #2). The `# SPEC AMBIGUITY:` comment in `foreground_model.py` must be resolved at that time.

---

## PR #8 — Write fixture thinking_pause_001 (ROADMAP Task 8)  (reviewed 2026-05-13)
**ROADMAP task:** 8
**Verdict:** APPROVE-WITH-NITS

**Scope check:** PR adds exactly three items — `companion_harness/fixtures/thinking_pause_001/case.json`, `companion_harness/fixtures/loader.py`, and `tests/conftest.py`. No adapter-module changes. `test_thinking_pause.py` remains a `pytest.skip` stub (Task 9 not written here). Scope is clean.

**Spec fidelity — Part 6c:**
- `case_id`, `stage`, `scenario`, `modalities`, `sensitivity`, `expected_events` match the Part 6c manifest entry verbatim. PASS.
- `expected_events` list matches Part 6c exactly: `[user_speech_start, silence_1500ms, no_full_response, user_speech_continuation]`. PASS.
- `fixture_ref` path correctly points to the fixture directory. PASS.
- `expected_metrics: {"thinking_pause_false_positive_rate": "= 0"}` is consistent with the v0.1a numeric gate in ROADMAP. PASS.
- `consent_class: "synthetic_eval"` — reasonable string; no spec constraint on valid values. PASS.

**Schema conformance — EvaluationCase:**
The JSON has 11 keys; `EvaluationCase` has 8 fields. Three extra keys — `sensitivity`, `description`, `signal_trace` — are not in the `EvaluationCase` dataclass. The loader returns a raw `dict` (correct), but the loader docstring claims "callers may construct an EvaluationCase from it," which is false: `EvaluationCase(**load_fixture("thinking_pause_001"))` raises `TypeError` due to the extra keys. See SHOULD-FIX below.

**Thinking-pause semantics — "stay silent through thinking pause":**
The `assert_no_full_response` marker at `t_ms=2300` is placed exactly when 1500ms of silence has elapsed (silence starts at `t=800ms`, `2300-800=1500ms`). The spec's scenario is "1.5s silence + continuation"; the gate checks that no `full_response` was emitted before continuation (which starts at `t=2400ms`). Placing the gate at `t=2300ms` (the end of the 1500ms silence span, before continuation) is the correct and faithful interpretation. The timing is right.

**Audio representation — synthetic VAD signal trace:**
The coder used a timestamped `vad_speech: bool` signal trace instead of PCM audio, citing that v0.1a has no real audio capture. This is a defensible interpretation. Part 6c specifies `modalities: [audio]` for the fixture but does not prescribe PCM — the spec says "recorded session + expected events or metrics." A synthetic VAD signal trace is sufficient for Task 9 to exercise the VAD→SpeakPolicy replay path: Task 9 feeds the `vad_speech` frames into `VADDetector` as scripted `p_speech` values, and the policy path runs deterministically from those signals. This interpretation correctly scopes to v0.1a capability (VAD-only, no real audio pipeline). Appropriate call.

**Findings:**

- [SHOULD-FIX] `companion_harness/fixtures/loader.py:12` — Docstring reads "callers may construct an EvaluationCase from it." This is false: `EvaluationCase(**load_fixture("thinking_pause_001"))` raises `TypeError` because the returned dict has three extra keys (`sensitivity`, `description`, `signal_trace`) that are not fields on the `EvaluationCase` dataclass. The Task 9 author will read this docstring and either write broken code or waste time debugging. Fix: change the docstring to "Returns the raw fixture dict. The dict may contain extra keys (e.g., `signal_trace`) beyond the `EvaluationCase` fields; callers should access needed keys directly."

- [CONCERN] `companion_harness/fixtures/thinking_pause_001/case.json:signal_trace[t=1000ms]` — Note reads "VAD may fire EOU candidate." At `t=1000ms`, silence has lasted only 200ms (`1000-800=200ms`), which is below the `_SILENCE_ONSET_MS=300ms` threshold in `VADDetector`. The VAD does NOT fire at this point. The note is factually incorrect and could mislead Task 9's author into placing an assertion at the wrong timestamp. The VAD EOU fires between `t=1000ms` and `t=1500ms` (somewhere around `t=1100ms`). Fix: change the note to "silence at 200ms — VAD threshold not yet reached (needs 300ms); no EOU signal yet."

- [CONCERN] Two entries in `signal_trace` share `t_ms=2300`: `vad_silence_active` and `assert_no_full_response`. Ordering is implied by JSON array position, but Task 9's replay logic must read these in array order, not by sort-stable timestamp. The intended ordering (silence frame first, then gate check) is unambiguous from the array, but deserves a comment. Fix: add a `notes` clarification to the `assert_no_full_response` entry that the gate applies to all events up to and including this timestamp, or split to `t_ms=2299` and `t_ms=2300`.

- [NIT] `assert_no_full_response` is not a spec-defined event_type. It is a fixture-internal gate convention that Task 9 will need to handle specially. Nothing in the fixture, loader, or conftest documents this convention. Task 9's author must deduce it from the `notes` field. Add a top-level `"fixture_conventions"` key to `case.json` (or a `README.md` in the fixture directory) explaining that event_type `assert_no_full_response` is a replay-gate marker, not a real event type, and that the test runner must not inject it into the event stream.

**"Fixture loads in pytest collection" success criterion:**
The conftest.py registers `thinking_pause_001` as a session-scoped pytest fixture, which makes the loader importable at collection time. However, `load_fixture()` is not actually invoked during collection — it is only called when a test requests the fixture. Since no test in this PR requests it, a malformed `case.json` would not be caught by `pytest --collect-only`. The success criterion is met in the minimal sense (fixture available at collection time), but is weaker than "fixture is loaded and validated during collection." Acceptable for Task 8; Task 9 will exercise the actual load.

**Minimality check:**
- `loader.py` is 16 lines; only JSON open + return. No try/catch, no schema validation, no invented helpers. Correct.
- `conftest.py` is 10 lines; one fixture, session scope. Correct.
- No speculative abstractions, no scope creep.

**Invariant checks:**
- Invariant #1 (no unlogged behavior): fixture is static data, not runtime code; not applicable.
- Invariant #5 (policy replay determinism): the `signal_trace` is deterministic by construction (scripted frames). PASS — the fixture will support deterministic replay in Task 9.
- Invariant #6 (behavioral tolerance): `expected_events` uses the behavioral event names from Part 6c, not text-similarity-based assertions. PASS.

**Watch-item 3 (SensitiveField.retention_policy_id non-empty):** No `SensitiveField` instances constructed. N/A. PASS.
**Watch-item 8 (VAD numeric defaults):** Not touched. Still active.
**Watch-item 9 (quiet_mode not as privacy_mode string):** Not touched. Still active.
**Watch-item 12 (ThinkerProposalGen):** Not touched. Still active.

**Cross-PR watch-items created:**
13. `case.json` contains three keys (`sensitivity`, `description`, `signal_trace`) not present in the `EvaluationCase` dataclass. Task 9 (`test_thinking_pause` implementation) must access `signal_trace` directly from the raw dict, NOT via `EvaluationCase(**fixture)`. Any future fixture loader refactor that attempts to deserialize into `EvaluationCase` must either (a) filter extra keys before construction, or (b) expand `EvaluationCase` to include `signal_trace` as an optional field. Do not silently construct with `**` kwargs.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` ambiguity must be resolved when Stage 6 begins.
13. (New) `load_fixture()` returns a dict with extra keys vs `EvaluationCase`; callers must access `signal_trace` directly from the dict, not via `EvaluationCase(**data)`.

---

## PR #8 — re-review round 2  (reviewed 2026-05-13)
**ROADMAP task:** 8
**Verdict:** APPROVE — PR #8 CONVERGED

**Fixes verified:**

- [SHOULD-FIX — RESOLVED] `companion_harness/fixtures/loader.py` docstring — No longer claims `EvaluationCase(**load_fixture(...))` works. New text: "Returns the raw fixture dict. Callers should access keys directly; the dict may contain extra keys beyond EvaluationCase fields (e.g. signal_trace)." Truthful. PASS.

- [CONCERN 1 — RESOLVED] `signal_trace[t=1000ms]` note — Now reads "silence at 200ms — below VAD's 300ms onset threshold; no EOU signal yet." Math is correct: silence began at t=800ms, so t=1000ms is 200ms of silence, below the `_SILENCE_ONSET_MS=300ms` threshold from `turn_detector_vad.py`. Factually accurate. PASS.

- [CONCERN 2 — RESOLVED] `assert_no_full_response` shifted to `t_ms=2301`. Ordering is now `2300 (vad_silence_active)` → `2301 (assert_no_full_response)` → `2400 (user_speech_continuation)`. Unambiguous array ordering without requiring sort-stable timestamp equality. PASS.

- [NIT — RESOLVED] `fixture_conventions` top-level key added to `case.json`. Text: "Replay-gate marker, not a real event that enters the event stream. Asserts that no full_response SpeakDecision occurs at or before its timestamp." Accurately describes the gate marker semantics. PASS.

**JSON validity and pytest collection:**
`case.json` is valid JSON (12 keys, confirmed `json.load` succeeds). `pytest --collect-only` collects 30 tests including `test_thinking_pause.py::test_thinking_pause` with no collection errors. Task 8 success criterion met. PASS.

**New findings:** none.

**Round-1 PASS items — all still intact:**
- Spec fidelity to Part 6c (case_id, stage, scenario, modalities, sensitivity, expected_events match verbatim): PASS.
- Schema conformance (`load_fixture` returns raw dict, not `EvaluationCase`): PASS.
- Stay-silent semantics (gate at t=2301ms, before continuation at t=2400ms): PASS.
- Synthetic-audio interpretation (VAD signal trace instead of PCM — scoped to v0.1a VAD-only baseline): PASS.
- Minimality (`loader.py` 17 lines, `conftest.py` 10 lines, no try/catch, no speculative helpers): PASS.
- Scope (3 new files only, no adapter-module changes): PASS.

**Watch-item 13 — UPDATED:** The reworded docstring directly addresses the extra-keys hazard at the documentation level — callers are now explicitly told to "access keys directly." The underlying structural gap (extra keys not in `EvaluationCase`) remains: any future refactor attempting `EvaluationCase(**load_fixture(...))` will still raise `TypeError`. Watch-item 13 remains active for Task 9 and any future fixture loader refactor. The docstring fix is necessary but not sufficient to close this watch-item; it closes when Task 9's implementation is verified to access `signal_trace` from the raw dict.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites. Spot-check on each new PR that constructs `SensitiveField`.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string in any future PR. Track until quiet-mode is spec-defined with its own field.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
13. (Active) `load_fixture()` returns extra keys vs `EvaluationCase` schema. Task 9 must access `signal_trace` from the raw dict, not via `EvaluationCase(**data)`. Docstring now warns callers; watch-item closes when Task 9 implementation is verified correct.

---

## PR #9 — Implement and pass test_thinking_pause (ROADMAP Task 9)  (reviewed 2026-05-13)
**ROADMAP task:** 9
**Verdict:** REQUEST-CHANGES

**Summary:** This is the first green contract test. It fails the primary review criterion: the test is vacuous. It passes by construction — the thinking-pause logic could be completely deleted and the test would still pass, because silence is forced by two independent invariants that have nothing to do with thinking-pause detection.

---

### BLOCKER findings

**[BLOCKER] `tests/test_thinking_pause.py:22` — `user_addressed_agent=False` hardcoded on ALL frames makes `full_response` structurally unreachable regardless of EOU state.**

Trace `decide()` for every frame in the fixture:
- Speech frames (`vad_speech=True`): `user_speaking=True` → gate 2 fires → `silence`. Thinking-pause logic never reached.
- Silence frames (`vad_speech=False`): `eou_prob=0.0` (see BLOCKER 2 below) → `eou_probability <= 0.5` → gate 3 fires → `silence`. Thinking-pause logic never reached.
- The only code path to `full_response` is: `eou_probability > 0.5` AND `user_addressed_agent=True`. With `user_addressed_agent` hardcoded `False`, that path is permanently blocked at gate 4 — `if inputs.user_addressed_agent:` is never true. Even if thinking-pause detection were completely broken (e.g. VAD fired EOU on every silence frame with `p_done=0.95`), `full_response` would still never be returned. The test cannot distinguish "stayed silent because thinking-pause was correctly recognized" from "stayed silent because `user_addressed_agent=False` short-circuits gate 4."

Fix: The thinking-pause scenario is specifically a case where the agent IS being addressed (the user is mid-thought and will complete a direct statement to the agent). The fixture should have `user_addressed_agent=True` during silence frames — representing that the user has previously addressed the agent and is mid-utterance — and the test must assert that silence is produced despite `eou_probability > 0.5` being producible (or potentially being produced erroneously). The correct contract to test is: "even when EOU fires during a thinking pause, the full policy path still yields silence because… [the VAD signal was a false EOU, or user_speaking context still holds]." With `user_addressed_agent=False`, the contract is untestable through this path.

**[BLOCKER] `tests/test_thinking_pause.py:73–83` — Fresh `VADDetector` per frame destroys the silence-accumulation state that is the entire mechanism by which thinking-pause detection works.**

`VADDetector` accumulates `_silence_ms` across frames — that is the EOU detection mechanism. Each call creates a new instance with `_silence_ms=0`. No TurnSignal can ever fire, because a single frame of 32ms silence never exceeds `silence_onset_ms=300ms`. The consequence:
- `signal` is always `None` in the loop.
- `eou_prob = p` (the fallback), which is always `0.0` for silence frames and `1.0` for speech frames.
- For silence frames: `eou_prob=0.0` → gate 3 → silence. But this is not because the system correctly recognized a thinking pause — it is because the accumulator was never given a chance to fire.
- **The VADDetector's EOU logic is never exercised at all.** The test exercises zero VAD behavior and zero SpeakPolicy behavior above gate 3.

This is the second independent way the test passes by construction. Even if BLOCKER 1 were fixed (`user_addressed_agent=True`), BLOCKER 2 would still make the test vacuous: EOU would never fire, so `full_response` would still never be produced, but for the wrong reason (state never accumulated, not that the pause was correctly recognized).

Fix: Create ONE `VADDetector` instance before the loop and feed all frames through it sequentially. The detector's state accumulates naturally, mirroring a continuous audio stream. The fixture's signal trace was explicitly designed for this (it encodes elapsed silence across frames). With a persistent detector, the EOU will fire after ~300ms of scripted silence, producing a TurnSignal with `p_done=1.0`. Then the test becomes meaningful: does the policy still return `silence` or does it erroneously emit `full_response`? (The answer depends on fixing BLOCKER 1 too.)

**[BLOCKER] `tests/test_thinking_pause.py:92–96` — `eou_prob=0.0` fallback when `signal is None` sets up inputs that trivially satisfy gate 3 (`eou_probability <= 0.5`) even during silence periods. This is a third independent path to vacuousness. Combined with fresh-detector-per-frame (BLOCKER 2), `eou_prob` is always 0.0 for silence frames, making the policy's gate 3 fire on every silence frame for a reason entirely unrelated to thinking-pause recognition.**

---

### Correctness findings

**[CONCERN] `tests/test_thinking_pause.py:55` — `EventLogger` is constructed but never started (`await logger.start()` is never called). The `_sink` coroutine is registered but the drain task is never launched. `EventLogger.log()` enqueues events into `self._queue`, but without a running drain loop, `queue.join()` would hang in `stop()` and any backpressure condition is undetected. For this test the logger is never stopped, so no hang occurs, but `logged` is never populated (drain loop not running). This means the `logged` list always remains empty — so any future assertion on emitted events would silently pass on zero events.**

The test does not currently assert on `logged`, so this does not affect the pass/fail outcome. But it indicates the test setup is incomplete for a contract test that is supposed to exercise event logging.

**[CONCERN] `tests/test_thinking_pause.py` — `assert_no_full_response` gate frames are fed into `_replay()` as regular frames (including through `VADDetector.process_frame()`). The fixture specifies these as non-real events that must not enter the event stream. The gate frames have `vad_speech=False`, so they pass through the loop as silence frames. This is benign today but will produce a spurious `vad_frame` event in the log for a frame that should not exist in the stream.**

---

### Style findings

**[NIT] `tests/test_thinking_pause.py:1` — `import asyncio` is imported but never used directly in the test file. `_sink` is defined as `async def` but uses no `await` — it could be a plain synchronous callable, and `asyncio` does not need to be imported.**

**[NIT] `tests/test_thinking_pause.py:99` — `_replay()` returns `{"t_ms": ..., "action_type": ...}` dicts. The determinism assertion `results_1 == results_2` compares dicts by value. Dict equality in Python is order-independent for keys but value-dependent. This is fine — the assertion is meaningful. However, the determinism test is trivially satisfied by the current implementation because the entire function is synchronous and stateless per-call (no wall clock, no random). The assertion is non-vacuous in principle but provides no regression value against a future non-deterministic bug that isn't also a Python runtime bug.**

---

### Watch-item checks

**Watch-item 13 (access `signal_trace` from raw dict, not `EvaluationCase(**data)`):** `data["signal_trace"]` at line `tests/test_thinking_pause.py:109`. Accesses the key directly from the raw dict. PASS. Watch-item 13 is RESOLVED.

**Watch-items 3, 8, 9, 12:** Not touched by this PR. Still active.

---

### Scope check

PR touches exactly one file: `tests/test_thinking_pause.py`. No adapter modules changed. Scope is clean.

---

### Cross-PR watch-items created

None. The blockers are entirely within this test file.

### Cross-PR watch-items — final state

- Watch-item 13: RESOLVED — `signal_trace` accessed as `data["signal_trace"]` from raw dict.

**Current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.

---

## PR #9 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 9
**Verdict:** REQUEST-CHANGES

**Summary of round-1 blockers and coder's claimed fixes:**
- BLOCKER 1: `user_addressed_agent=False` hardcoded → fixed to `True`.
- BLOCKER 2: Fresh `VADDetector` per frame → fixed to one persistent detector.
- BLOCKER 3: `eou_prob=0.0` fallback hardcoded → fixed to use `signal.p_done`.
- NIT: unused `import asyncio` → removed.
- The `assert_no_full_response` gate frame is now skipped before entering the loop.

**Replay-fidelity analysis — the critical question:**

The fixture encodes a real-world 1500ms thinking pause: silence begins at `t_ms=800`, the gate fires at `t_ms=2301`. The coder's own summary says "5 silence frames × 32ms/frame = 160ms of accumulated silence, which is below the 300ms threshold — the VAD never fires." This is exactly the compression problem described in the review prompt.

Trace the persistent detector through every signal_trace entry that enters the loop (9 entries; `assert_no_full_response` at `t_ms=2301` is skipped):

| Entry | t_ms | vad_speech | p | _silence_ms after frame | TurnSignal? |
|-------|------|-----------|---|--------------------------|-------------|
| 1 | 0 | True | 1.0 | 0 (reset) | None |
| 2 | 400 | True | 1.0 | 0 | None |
| 3 | 800 | False | 0.0 | 32 | None (32 < 300) |
| 4 | 1000 | False | 0.0 | 64 | None |
| 5 | 1500 | False | 0.0 | 96 | None |
| 6 | 2000 | False | 0.0 | 128 | None |
| 7 | 2300 | False | 0.0 | 160 | None (160 < 300) |
| 8 | 2400 | True | 1.0 | 0 (reset) | None |
| 9 | 2800 | False | 0.0 | 32 | None |

The VAD NEVER fires. There are 5 silence frames (entries 3–7) each adding one `frame_duration_ms=32` tick to `_silence_ms`. Maximum accumulated silence: 160ms, well below `silence_onset_ms=300ms`. `eou_prob` is always `0.0` for all frames. With `eou_probability=0.0 <= 0.5`, gate 3 of `decide()` fires silence on every silence frame. The test passes — but STILL by dodging the mechanism.

**Root cause: the fix is structurally correct but architecturally insufficient.** A persistent detector does accumulate state across frames. The test now correctly threads the same detector. But the fixture has only 5 silence-period entries, each treated as a single 32ms frame. Five frames × 32ms = 160ms, not 1500ms. The fixture timestamps (800ms, 1000ms, 1500ms, 2000ms, 2300ms) are LOGICAL MARKERS of what the real elapsed time would be in a live session, not a specification that each gap is 32ms wide.

---

### BLOCKER finding

**[BLOCKER] `tests/test_thinking_pause.py:77–100` — Compression: the fixture's 1500ms silence window is replayed as 5 × 32ms = 160ms, so the VAD never fires. The EOU path is never exercised. The test still passes vacuously.**

A faithful replay of the fixture's real 1500ms silence would require feeding approximately 47 silence frames (1500ms / 32ms ≈ 47). With a persistent detector, the VAD fires a TurnSignal after the 10th frame (10 × 32ms = 320ms ≥ 300ms `silence_onset_ms`). At that point `eou_prob = signal.p_done = 1.0`. With `user_addressed_agent=True` (fixed in round 2) and `eou_probability=1.0 > 0.5`, `decide()` returns `full_response`. A faithful-replay test would FAIL at the contract assertion.

This is not fixable by the coder alone. It exposes a genuine adapter/spec gap (see ESCALATION below).

---

### ESCALATION — not a test fix, must go to project lead

**A faithful replay of thinking_pause_001 produces `full_response` during the pause. The thinking-pause contract is NOT satisfiable with the current merged v0.1a adapters (`VADDetector` + `SpeakPolicy`).**

Mechanism: `VADDetector` with `silence_onset_ms=300` fires EOU after 300ms of silence. In the thinking-pause scenario, 1500ms of silence contains 1200ms of post-threshold silence during which EOU is confirmed (`p_done ≈ 1.0`). `SpeakPolicy.decide()` has no code path that suppresses `full_response` given `eou_probability > 0.5` AND `user_addressed_agent=True` — that is the exact condition for `full_response`. There is no "user mid-thought" signal in `PolicyInputs`.

The spec (Part 6 Stage 1) states the test scenario and expected outcome but does NOT describe the mechanism by which a VAD-only baseline distinguishes a 300ms end-of-turn from a 1500ms thinking pause. Part 8 lists `test_thinking_pause` as required at v0.1a but provides no mechanism hint. The spec note at Part 3 Stage 1 says "EOU decision window: accumulated current-turn audio. Smart Turn is invoked at silence-candidate moments" — which implies SmartTurnDetector (deferred to v0.1b) may be the actual mechanism for thinking-pause detection. If so, the thinking-pause contract is unsatisfiable at v0.1a with VAD-only, and either:

(a) `silence_onset_ms` must be set >> 1500ms (e.g., 2000ms), which would handle this fixture but falsify normal EOU detection for short turns, or
(b) The spec must explicitly state that `test_thinking_pause` requires v0.1b (SmartTurnDetector) not v0.1a (VAD-only), or
(c) A `user_mid_thought` or contextual-speech signal must be added to `PolicyInputs` and the fixture.

This is a **spec question and architecture decision**, not something the Task 9 test author can resolve unilaterally. The test should be marked `pytest.skip("blocked: thinking_pause contract unsatisfiable with VAD-only v0.1a — see spec gap issue #X")` until the project lead resolves which of (a)/(b)/(c) applies.

---

### Additional findings

**[CONCERN] `tests/test_thinking_pause.py:43–102` — `_replay()` calls `EventLogger` but never calls `await logger.start()`, so the drain loop never runs. This was noted as a CONCERN in round 1 (coder did not address it). With a persistent detector, this still applies: `logged` is always empty, and any future assertion on logged events would silently pass on zero events. Not a blocker now because no such assertion exists, but the test setup is structurally incomplete for a contract test that exercises event logging.**

**[NIT] `tests/test_thinking_pause.py:121` — `assert len(pre_gate) > 0` is correct and non-vacuous (pre_gate will have 7 entries). Good — this was the right fix from round 1's implicit concern about an empty pre-gate window. No finding.**

---

### Prior round-1 fixes — status

- BLOCKER 1 (`user_addressed_agent=False`): FIXED structurally — `True` in all frames. But the fix now exposes the escalation: with `True`, a faithful EOU fire returns `full_response`.
- BLOCKER 2 (fresh detector per frame): FIXED structurally — one persistent detector. But the fixture compression means the detector still never fires.
- BLOCKER 3 (`eou_prob=0.0` fallback): FIXED — now uses `signal.p_done`. Correct but irrelevant since `signal` is always `None`.
- NIT (unused asyncio import): FIXED — absent from file.
- CONCERN (logger never started): NOT ADDRESSED. Carries forward.

---

### Watch-item checks

- Watch-item 3 (SensitiveField non-empty): No SensitiveField constructed. PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.

---

### Cross-PR watch-items created

14. (ESCALATION — project lead required) `test_thinking_pause` contract is unsatisfiable with VAD-only v0.1a adapters. A faithful 1500ms silence replay fires the VAD (after 300ms), and `SpeakPolicy` has no mechanism to suppress `full_response` once EOU is confirmed and `user_addressed_agent=True`. Project lead must decide: (a) raise `silence_onset_ms` >> 1500ms (breaks short-turn EOU), (b) move `test_thinking_pause` to v0.1b scope (requires SmartTurnDetector), or (c) add a `user_mid_thought` signal to `PolicyInputs` and the fixture. Until resolved, Task 9 test must be a `pytest.skip` with a documented reason.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
14. (NEW — ESCALATION) Thinking-pause contract unsatisfiable with VAD-only v0.1a. Project lead must resolve (a)/(b)/(c) before Task 9 can ship a non-vacuous test.

---

## PR #9 — re-review round 3 (resolution: deferred to v0.1b)  (reviewed 2026-05-14)
**ROADMAP task:** 9
**Verdict:** APPROVE

**Context:** Project lead resolved watch-item 14 via GitHub issue #10: apply the spec's own Part 8 rule ("do not gate v0.1a on a capability v0.1a intentionally lacks") and defer `test_thinking_pause` to v0.1b alongside `test_backchannel_survival` / `test_detector_ablation`. Task 9 outcome changed from "first green test" to "documented deferral." PR #9 round 3 implements that deferral.

**Commit verified:** `c31ec2d` — touches exactly three files: `tests/test_thinking_pause.py`, `ROADMAP.md`, `CLAUDE.md`. No adapter modules touched. No fixture touched. Spec frozen at `docs/architecture-v0.1.md` (md5 `0107df2b` — identical to HEAD commit; file untouched). PASS.

---

### Change 1 — tests/test_thinking_pause.py

**Shape check vs canonical skip stubs (test_barge_in.py, etc.):** Clean match. All skip stubs have the same shape: module docstring, `import pytest`, one bare function, `pytest.skip(...)` as the sole statement. The new stub follows this pattern exactly.

**Skip reason check:** Accurate and complete. States (a) the mechanism required (`SmartTurnDetector`), (b) why VAD-only cannot satisfy it (cannot distinguish thinking pause from EOU), (c) the precedent (same rationale Part 8 uses for `test_backchannel_survival` / `test_detector_ablation`), and (d) the issue reference (`issue #10`). No vacuous remnant code, no dead imports. PASS.

**Module docstring check:** Cites `§Part 6 Stage 1`, the fixture (`thinking_pause_001`), the v0.1a acceptance gate, and "Deferred to v0.1b — see issue #10." Accurate and honest. PASS.

**pytest result:** `test_thinking_pause SKIPPED` — not passes, not errors. Confirmed: 23 passed, 7 skipped. Suite is back to the pre-Task-9 baseline. PASS.

---

### Change 2 — ROADMAP.md

**Task 9 annotation:** `[DEFERRED to v0.1b — see issue #10]` appended inline. Surgical — no restructuring, no reformatting, no surrounding lines touched. Matches the file's existing annotation style (`test_backchannel_survival` / `test_detector_ablation` use the same bracket-annotation pattern). PASS.

**v0.1a numeric gate:** `thinking_pause_false_positive_rate` row annotated `[v0.1b-gated — see issue #10]`. Annotation is inline in the table, consistent with table style. PASS.

**v0.1a pinned criterion:** "wait through thinking pauses" parenthetically annotated `(v0.1b-gated — see issue #10)`. Accurate — reflects the deferral without rewriting the sentence. PASS.

**v0.1a milestone paragraph:** Same annotation in the intro paragraph. Consistent. PASS.

No restructuring, no unrelated edits. Every annotation traces to issue #10. PASS.

---

### Change 3 — CLAUDE.md

**Pinned criterion annotation:** "wait through thinking pauses" parenthetically annotated `(deferred to v0.1b — see issue #10)`. One-line surgical edit inside the blockquote. Style matches the file. No surrounding lines touched. PASS.

---

### Spec / adapters / fixture untouched

- `docs/architecture-v0.1.md`: md5 checksum identical to HEAD. Not modified. PASS.
- `companion_harness/`: directory listing shows all files from prior PRs; no diff against HEAD. PASS.
- `companion_harness/fixtures/thinking_pause_001/`: fixture unchanged. Remains the v0.1b fixture as designed. PASS.

---

### One-PR-one-outcome judgment

CLAUDE.md rule 4 says one PR ships exactly one of: (a) stub, (b) skip-to-green, (c) docs update. This PR is a deferral: the test file is a stub (option a), and ROADMAP.md + CLAUDE.md are documentation (option c). Bundling these three files is acceptable because they are all expressions of a single atomic project-lead decision ("defer test_thinking_pause to v0.1b per issue #10"). Each file would be incoherent or misleading without the other two: a stub with no ROADMAP annotation leaves a dangling required-test entry; a ROADMAP annotation with no stub reversion leaves a vacuous "passing" test in the suite. The three changes are inseparable facets of one outcome. PASSES rule 4 under the deferral framing.

---

### Watch-item 14 — RESOLVED

The escalation from round 2 is fully addressed: project lead decision documented in issue #10, deferral applied per spec's own Part 8 rule. Watch-item 14 is closed.

---

### New cross-PR watch-item created

15. (v0.1b — ACTIVE) `test_thinking_pause` is deferred. When `SmartTurnDetector` lands in v0.1b, this test must be un-skipped and wired through it. The `thinking_pause_001` fixture remains the target fixture. The skip reason in `tests/test_thinking_pause.py` documents exactly what is needed. Reviewer of the Task 9 v0.1b PR must: (a) confirm `pytest.skip` is removed entirely (not just commented out); (b) confirm the test feeds the fixture through `SmartTurnDetector` (not `VADDetector`); (c) confirm the EOU path is genuinely exercised (not compressed as in rounds 1–2); (d) confirm the test fails if thinking-pause suppression is broken.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
14. RESOLVED — watch-item 14 closed; project lead decided deferral via issue #10.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands. See full criteria above.

---

## PR #11 — Implement and pass test_barge_in (ROADMAP Task 10)  (reviewed 2026-05-14)
**ROADMAP task:** 10
**Verdict:** REQUEST-CHANGES

**Summary:** The test has one blocker (the p95 index is off-by-one), two concerns (orphan assertion is vacuous for start_generation event; `physical_user_speech_onset_to_stop_ms_p95` gate is not measured at all), and the central design question about VAD detection must be resolved — but resolves cleanly in the coder's favor once the spec's own parenthetical is read.

---

### Central design question: does the test bypass VADDetector?

**Verdict: the bypass is spec-compliant, but the coder's stated reason is wrong on a material point.**

The coder's claim: "VADDetector.process_frame emits TurnSignal on end-of-utterance (silence onset), not on speech onset — no speech-onset signal is available."

Verified against `companion_harness/turn_detector_vad.py`:
- `process_frame()` returns `TurnSignal | None`. It returns `None` on every frame where speech probability is above threshold — i.e., it returns nothing during speech, only after silence accumulates to ≥ `silence_onset_ms`.
- There is no callback, no event emission, and no return value that signals *speech onset* (rising edge from non-speech to speech). The `_in_speech` flag is set internally but not exposed.
- `vad_frame` events are logged for every frame, but they carry no speech-onset semantics — they are frame-level probes, not onset signals.

**The coder's factual claim is correct: VADDetector has no speech-onset detection interface.** A caller cannot use it to detect the moment the user starts speaking — only the moment they stop (EOU).

However: the spec's own definition of the metric at Part 8 line 922-924 reads: "`vad_detected_user_speech_to_stop_ms` ... strictly gated — **measures the AudioOutputController stop path**." The spec itself defines this metric as a measure of the stop path, not the VAD detection path. The test measures from a caller-supplied `vad_event_id` (standing in for the VAD detection moment) to `play()` task completion — which IS the stop path. This aligns with the spec's own intent.

**The fixture also confirms this.** `barge_in_001/case.json` encodes `user_speech_onset` and `assistant_audio_stop_requested` at the same `t_ms=200` — the fixture treats speech onset as a precondition (a moment in time) and the gate as what follows immediately. The test correctly reproduces this by making `request_stop()` the clock start and `play()` completion the clock stop.

**Conclusion: the test measures exactly what the spec defines `vad_detected_user_speech_to_stop_ms` to measure.** The bypass of VADDetector for the detection half is an adapter gap (VADDetector genuinely cannot detect speech onset), but that gap does not make the test incorrect — the metric spec explicitly scopes the gate to the stop path. This is not analogous to test_thinking_pause (which required a mechanism VAD cannot provide); here the spec and the test are aligned. **No escalation required for this adapter gap.**

The `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate, however, is not measured at all (see CONCERN below).

---

### BLOCKER findings

**[BLOCKER] `tests/test_barge_in.py:119` — p95 index is off-by-one.**

```python
p95_idx = int(0.95 * len(latencies_ms))
p95_ms = latencies_ms[p95_idx]
```

With `n_trials=30`, `int(0.95 * 30) = 28`. `latencies_ms[28]` is the 29th element (0-indexed) of a 30-element sorted list. The correct p95 of 30 samples is the value at or above 95% of the distribution — the 29th value (index 28) is the 97th percentile, not the 95th. The standard formula for the pth percentile index (ceiling method) is `ceil(p * n) - 1`, which for p=0.95, n=30 gives `ceil(28.5) - 1 = 29 - 1 = 28`. By coincidence the result is the same here — but `int(0.95 * n)` gives index 28 for n=30 whereas the correct ceiling gives 28 as well. However for n=20: `int(0.95 * 20) = 19`, which is index 19 — out-of-bounds (list has indices 0–19). For n=21: `int(0.95 * 21) = 19`, which is correct. The formula is wrong: `int(0.95 * n)` produces an index equal to `n` when `0.95 * n` is exactly an integer (e.g. n=20, n=40, n=100), causing an `IndexError`. With n=30 it works by accident (`int(28.5) = 28`), but the formula is broken in general.

Fix: `p95_idx = min(int(0.95 * n_trials + 0.5), n_trials - 1)` or more idiomatically use `statistics.quantiles(latencies_ms, n=20)[18]` (the 95th percentile in 20-quantile space). Minimum correct replacement: `p95_idx = min(int(0.95 * len(latencies_ms)), len(latencies_ms) - 1)`.

---

### Concern findings

**[CONCERN] `tests/test_barge_in.py:111–115` — orphan assertion vacuously passes for `assistant_generation_start`.**

```python
for evt in received:
    assert evt.caused_by, (...)
```

`start_generation(caused_by=["policy-decision-001"])` passes a hardcoded literal string `"policy-decision-001"` that is not a real event_id from any logged event. The `caused_by` list is non-empty (so `assert evt.caused_by` passes), but the causal reference is a phantom — it points to an event that was never logged. The orphan assertion here checks structural non-emptiness, not causal closure. The Stage 0 invariant (DAG must close) requires that every `caused_by` reference resolves to a real event in the log. This assertion does not test that.

This is a concern rather than a blocker because: (a) a full causal-graph check is `test_causal_graph_completeness` (Task 16), not this test; and (b) the "policy-decision-001" sentinel mirrors the pattern used in prior unit tests (`test_audio_output_controller.py`). However, the comment "No orphan events (invariant #1)" overclaims — this assertion does not verify invariant #1 at the causal-graph level, only that `caused_by` is non-empty.

Fix: change the comment to "Verify caused_by is non-empty (structural check; full DAG closure verified in test_causal_graph_completeness)."

**[CONCERN] `tests/test_barge_in.py` — `physical_user_speech_onset_to_stop_ms_p95` gate is not measured.**

ROADMAP Task 10 success criterion is "VAD-to-stop p95 < 200ms on the fixture." The ROADMAP specifies two separate hard gates: `vad_detected_user_speech_to_stop_ms_p95 < 200ms` AND `physical_user_speech_onset_to_stop_ms_p95 < 350ms`. The test measures only the first. In a pure unit-test context this is defensible (the physical onset gate requires a real audio pipeline), but the test neither measures nor stubs the second gate, nor does it document why it is omitted. At minimum a `# physical_user_speech_onset_to_stop_ms_p95 not measured here — requires live audio pipeline` comment should be present. This is a concern because both gates are listed as hard pass/fail in the ROADMAP.

---

### Non-vacuousness check

- `assert p95_ms < 200` — this WILL FAIL if `AudioOutputController.play()` does not honor `request_stop()` promptly. With a 10ms-per-chunk sink and a stopped stop event, the first chunk is already in-flight; the stop fires after the 5ms sleep, before the second chunk. Measured latency will be ~5ms on a fast machine. Non-vacuous: if `play()` ignored the stop flag and drained all 3 chunks (30ms), it would still pass — but if the stop flag were never checked (e.g., an infinite loop), it would fail. The test would catch a completely broken stop path. PASS.
- `assert vad_event_id in stop_req.caused_by` — non-vacuous; relies on the caller passing the correct `caused_by` to `request_stop()`. PASS.
- `assert not controller.is_playing` — non-vacuous. PASS.
- `assert "assistant_audio_stop_completed" in event_types` — non-vacuous; only emitted by `play()` on the stop path. PASS.

---

### Fixture conformance (case.json)

- `case_id: "barge_in_001"` — matches spec Part 6c manifest entry. PASS.
- `stage: 1` — correct. PASS.
- `scenario: "assistant_interrupted"` — matches Part 6c. PASS.
- `expected_metrics: {"assistant_stop_latency_ms_p95": "<200"}` — this is a renamed version of the spec gate `vad_detected_user_speech_to_stop_ms_p95`. The fixture uses a shorter key name while the ROADMAP uses the full qualified name. The description field correctly expands the semantics. Acceptable. PASS.
- `expected_events` includes `user_speech_onset` — this is not a spec-defined event_type from Part 5. It is a fixture convention (like `assert_no_full_response` in thinking_pause_001). The fixture does not include a `fixture_conventions` key explaining this (unlike thinking_pause_001 round 2 which added one per PR #8). Minor inconsistency with established fixture discipline.
- `signal_trace[t_ms=200]` has two entries at the same timestamp: `user_speech_onset` and `assistant_audio_stop_requested`. Same ordering hazard flagged and resolved in PR #8 (thinking_pause_001). The thinking_pause_001 fix was to shift the gate to `t_ms=2301`. Here the two entries are at the same timestamp with no `fixture_conventions` comment. Low severity since the test doesn't replay the signal trace timestamp-by-timestamp (it uses wall-clock measurement, not fixture timestamp replay), but it violates the established fixture discipline from PR #8.

---

### Style / scope check

- Scope: PR adds `tests/test_barge_in.py` (converted from skip) and `companion_harness/fixtures/barge_in_001/case.json`. No adapter module changes. PASS.
- No model SDK imported anywhere. PASS.
- `import statistics` is used (for `statistics.median`). PASS.
- `sink_delays` list is populated in `audio_sink` but never read or asserted on. Dead state — `_delays.append(0.01)` is a no-op from the perspective of any test assertion. Delete both the `sink_delays: list[float] = []` declaration and the `_delays.append(0.01)` line inside `audio_sink`.
- `_make_logger()` is a single-use helper (one caller). Per CLAUDE.md rule 2 ("No abstractions for single-use code"), this is a mild scope-creep item — but the pattern matches prior test files (`test_audio_output_controller.py` uses the same helper shape), so style consistency overrides here. Not a finding.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed in this PR. PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched. Still active.

---

### Cross-PR watch-items created

16. (New) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` is a hard ROADMAP gate that is not measured in `test_barge_in` and not measured anywhere in the test suite. This gate requires a live audio pipeline or a fixture that encodes real VAD detection latency (not just post-detection stop latency). Track until either: (a) a separate test or metric collection covers this gate, or (b) the project lead explicitly defers it alongside `test_thinking_pause` with a documented reason. The ROADMAP currently lists both barge-in gates as hard pass/fail for v0.1a.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (New) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate is unmeasured. Track until covered or explicitly deferred by project lead.

---

## PR #11 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 10
**Verdict:** APPROVE — PR #11 CONVERGED

**All 5 round-1 findings verified:**

- [BLOCKER — RESOLVED] `tests/test_barge_in.py:122` — `p95_idx = min(int(0.95 * len(latencies_ms)), len(latencies_ms) - 1)` is present. The `min(...)` clamp is the correct defense against any `n` where `int(0.95 * n)` could equal `n` (though for exactly `n_trials=30` the old formula was safe by arithmetic accident — the fix is still strictly correct for the general case and the guard is sound for any future change to `n_trials`). PASS.

  Note: the round-1 BLOCKER analysis contained a reasoning error — `int(0.95 * 20) = 19` is actually in-bounds for a 20-element list (valid indices 0–19). The old formula was never literally out-of-bounds for the multiples of 20 cited (n=20, 40, 100 all produce `int(0.95*n) = n-1`, which is a valid last-element index). The fix is correct regardless: `min(..., n-1)` is a proper defensive clamp and the formula is now robust for any `n`. The blocker was overclaimed but the fix is still the right call.

- [CONCERN 1 — RESOLVED] `tests/test_barge_in.py:114` — Comment reads "Verify caused_by is non-empty (structural check; full DAG closure verified in test_causal_graph_completeness)." The overclaiming "No orphan events (invariant #1)" text is gone. Scoped and accurate. PASS.

- [CONCERN 2 — RESOLVED] Module docstring (lines 8–12) now explicitly documents that `physical_user_speech_onset_to_stop_ms_p95 < 350ms` is out of scope for this fixture-driven contract test, and states the reason (requires real-audio integration / b200 / Task 17). Accurate per spec Part 8 which defines `vad_detected_user_speech_to_stop_ms_p95` as measuring the AudioOutputController stop path. PASS.

- [NIT 1 — RESOLVED] No reference to `sink_delays`, `_delays`, or any list appended in `audio_sink` beyond the `await asyncio.sleep(0.01)` call. Dead state fully removed. PASS.

- [NIT 2 — RESOLVED] `signal_trace` entries split to `t_ms=200` (`user_speech_onset`) and `t_ms=201` (`assistant_audio_stop_requested`). `fixture_conventions` key is present and valid JSON. PASS.

**New findings:**

- [NIT] `companion_harness/fixtures/barge_in_001/case.json:fixture_conventions` — The description for `assistant_audio_stop_requested` reads "not a real logged event_type." This is inaccurate: `assistant_audio_stop_requested` IS a real Part 5 event_type that IS logged by `AudioOutputController` and asserted in the test (it appears in `expected_events`). The intended meaning is that the *signal_trace entry at t_ms=201* is a fixture-internal gate marker, not that the event_type string is synthetic. Compare to `thinking_pause_001`'s use of `assert_no_full_response`, which genuinely is not a real event_type. The wording should be "The signal_trace entry at t_ms=201 for this event_type is a fixture-internal gate marker, not an injected event; the event_type itself is real and will appear in the logged stream." This is a NIT — the test is unaffected, and any reader who cross-references `expected_events` will resolve the ambiguity. Does not block merge.

**Round-1 PASS items — all still intact:**
- Spec-compliant detection-half scoping (test measures AudioOutputController stop path, per spec Part 8 definition). PASS.
- Non-vacuous assertions (`assert p95_ms < 200`, `assert vad_event_id in stop_req.caused_by`, `assert not controller.is_playing`, `assert "assistant_audio_stop_completed" in event_types`). PASS.
- Faithful replay (wall-clock measurement from `request_stop()` to `play()` task completion matches the fixture's intent). PASS.
- Schema conformance (`case.json` valid JSON, all required fields present). PASS.
- No model SDK import (imports are stdlib + `companion_harness.*` only). PASS.
- Only `tests/test_barge_in.py` and `companion_harness/fixtures/barge_in_001/case.json` touched. No scope creep. PASS.

**Holistic pass — ROADMAP Task 10:**
- Task 10 success criterion: "VAD-to-stop p95 < 200ms on the fixture." The test runs 30 trials, measures from `request_stop()` to `play()` task completion, and asserts `p95_ms < 200`. Non-vacuous. The `pytest.skip` is gone; the test is a live assertion. PASS.
- CLAUDE.md rule 4(b) satisfied: a `pytest.skip` is converted to a passing test. PASS.
- Invariant #1 (no unlogged behavior): all emitted events carry non-empty `caused_by`; `stop_requested` is causally linked to the VAD event. PASS (structural check; full DAG closure in Task 16).

**Watch-item 16 — state confirmed ACTIVE:**
The module docstring correctly documents that `physical_user_speech_onset_to_stop_ms_p95 < 350ms` is out of scope for this test and explicitly names "b200 / Task 17 end-to-end ReplayRun" as the required vehicle. This is adequate documentation tracking of the gap. Watch-item 16 remains ACTIVE until Task 17 ships or the project lead explicitly defers it alongside `test_thinking_pause`. The docstring is the living record.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope. Track until Task 17 ships or project lead defers.

---

## PR #12 — Implement and pass test_explicit_turn_handoff (ROADMAP Task 12)  (reviewed 2026-05-14)
**ROADMAP task:** 12
**Verdict:** REQUEST-CHANGES

**Summary:** The test is substantively correct and the contrast case is non-vacuous. Two BLOCKER-level issues: (1) ROADMAP Task 11 (`test_direct_question_latency`) was skipped in violation of CLAUDE.md rule "Pick the lowest-numbered task that isn't done. Do that one. Stop." — Task 12 must not be merged until Task 11 ships or the project lead explicitly defers it. (2) The contrast case's `social_mode` is wrong for the scenario it claims to test, which creates a scenario-label / mechanism mismatch that could mislead future maintainers and leaves the `social_mode` gate (gate 1) untested by this test. Additionally, the fixture is missing a `fixture_conventions` key (established discipline from PR #8) and two `expected_events` strings are invented, non-spec event_types.

---

### BLOCKER findings

**[BLOCKER] ROADMAP task ordering violation — CLAUDE.md rule: "Pick the lowest-numbered task that isn't done. Do that one. Stop."**

ROADMAP Task 11 (`test_direct_question_latency`) has no corresponding GitHub PR and no ledger entry. Task 12 is submitted before Task 11 is done. CLAUDE.md is unambiguous: the lowest-numbered incomplete task must be shipped first. No exception for "easier" or "logically independent" tasks. This PR must be held until Task 11 ships, OR the project lead explicitly defers Task 11 (as was done for Task 9 via issue #10). Neither has happened.

Note: the coder's local-vs-b200 determination for Task 12 is correct (see below). The test is a decision-correctness test, not a latency test. But correctness of the current PR does not justify the sequencing violation.

**[BLOCKER] `tests/test_explicit_turn_handoff.py:66–76` — Contrast case `social_mode` is wrong for the stated scenario, causing a mechanism mismatch.**

The contrast fixture note reads "user finishes speaking to someone else." In that scenario, `social_mode` is `"user_addressing_other"`, which is in `_BLOCKING_SOCIAL_MODES`. Silence would come from gate 1 of `decide()` — the hard social-mode block — before reaching gate 4 (the `user_addressed_agent` check). However, `_base_inputs` hardcodes `social_mode="user_addressing_agent"`, so the contrast case actually simulates "user spoke but didn't address the agent" — a different scenario, one where silence comes from gate 4 (addressed check) not gate 1 (social mode). The contrast case does correctly prove that EOU alone does not cause speech — that is non-vacuous and correct. But the scenario mismatch means:

(a) The note "user finishes speaking to someone else" is factually wrong for the inputs used.
(b) Gate 1 (`_BLOCKING_SOCIAL_MODES`) is not exercised by this test.
(c) If a future refactor changed the decision logic so that `user_addressed_agent=False` no longer suppresses speech but `social_mode` still did, this test would break (revealing the bug) — but the scenario label would mislead the fixer into thinking the test intended to exercise the social_mode gate.

Fix: Either (a) change the contrast fixture note to "EOU confirmed but agent not explicitly addressed (social_mode = user_addressing_agent)" to match the actual inputs, OR (b) use `social_mode="user_addressing_other"` in the contrast inputs (and update both the fixture and _base_inputs call for the contrast case). Option (a) is simpler and keeps the test focused on the `user_addressed_agent` discriminator as clearly as the handoff case. If option (b) is chosen, a separate assertion that silence comes from gate 1 (NOT from gate 4) should be added, or the two scenarios should be split into separate sub-cases.

---

### SHOULD-FIX findings

**[SHOULD-FIX] `companion_harness/fixtures/explicit_turn_handoff_001/case.json` — Missing `fixture_conventions` key for `contrast_trace`.**

PR #8 established the discipline: non-standard fixture keys must be explained in a `fixture_conventions` top-level entry. The `contrast_trace` key is not in `EvaluationCase` schema, is not in `signal_trace` (the documented extension point), and is used by the test but explained nowhere in the fixture. Future readers and the Task 14 (`test_policy_replay_exact`) author need to know what `contrast_trace` means and that it is not part of the replay stream. Add a `fixture_conventions` key with an entry explaining `contrast_trace` is a negative test case, not an event to inject into the event stream.

**[SHOULD-FIX] `companion_harness/fixtures/explicit_turn_handoff_001/case.json:8–13` — `expected_events` contains `"eou_confirmed"` and `"full_response_decision"` which are not spec-defined Part 5 event_types.**

The canonical Part 5 event_type list (audio domain) includes names like `assistant_generation_start`, `assistant_audio_stop_requested`, etc. `eou_confirmed` and `full_response_decision` are not in the spec. If these are intended as fixture-gate markers (like `assert_no_full_response` in `thinking_pause_001`), they must be documented in `fixture_conventions`. If they are intended as real event_types that should be logged by some adapter, the adapter must be named and the event_type must first be defined in the spec. As written, they create a fixture `expected_events` list that no adapter can currently satisfy, which will cause `test_policy_replay_exact` (Task 14) to report every run as incomplete against this case.

---

### NIT findings

**[NIT] `tests/test_explicit_turn_handoff.py:47` — `fixture["signal_trace"][0]` (the `user_speech_start` frame at `t_ms=0`) is loaded but never used. The test uses only index [1]. Dead read.**

**[NIT] `tests/test_explicit_turn_handoff.py:15–33` — `_base_inputs` helper has exactly 2 callers. CLAUDE.md rule 2 says "No abstractions for single-use code." Two callers is borderline; the helper's only value is avoiding repeating 14 identical keyword arguments. The inlining cost would be two verbose PolicyInputs constructors, which is arguably worse than the helper. Not raising as a blocker. Call it a judgment call; the pattern matches prior test files (`test_speak_policy.py` uses the same helper shape).**

---

### What the test gets right

- **Local vs b200 determination — CORRECT.** `test_explicit_turn_handoff` has no latency gate in the ROADMAP numeric gates table. The spec annotation is "must respond promptly" (decision-correctness: `full_response` must be chosen), not a sub-millisecond latency gate like `direct_question_latency_p50 < 800ms`. Compare: `test_direct_question_latency` explicitly maps to the `direct_question_latency_p50` / `p95` numeric gates and requires b200. `test_explicit_turn_handoff` maps only to a policy decision outcome. The local/stub-adapter determination is correct.

- **Contrast case is genuinely non-vacuous.** The contrast case (`eou_probability=0.88`, `user_addressed_agent=False`) reaches gate 4 of `decide()`, takes the fallthrough branch, and returns `silence(NOT_ADDRESSED_TO_AGENT)`. A policy implementation that returned `full_response` whenever `eou_probability > 0.5` (ignoring `user_addressed_agent`) would FAIL this contrast assertion. The test would catch that regression. PASS.

- **Handoff case asserts `USER_ADDRESSED_AGENT` in supporting_reason_codes.** This is a spec-correct assertion: the merged `speak_policy.decide()` puts `USER_ADDRESSED_AGENT` in `supporting_reason_codes` at the `full_response` branch. Non-vacuous.

- **`caused_by` threading.** `decision.caused_by == ["eou-signal-001"]` is asserted. Non-vacuous: would fail if `decide()` dropped the causal chain.

- **Spec fidelity of ReasonCodes.** `EOU_CONFIRMED` as primary and `USER_ADDRESSED_AGENT` as supporting for the handoff case, `NOT_ADDRESSED_TO_AGENT` as primary for the contrast case — all are the exact codes that `speak_policy.decide()` produces for these inputs. No invented ReasonCode strings.

- **Adapter purity.** Imports are `companion_harness.*` only. No model SDK. PASS.

- **Scope.** Only `tests/test_explicit_turn_handoff.py` and `companion_harness/fixtures/explicit_turn_handoff_001/case.json` are touched. No adapter modules changed. No test files for prior tasks modified. PASS.

- **`user_addressed_agent` is the correct PolicyInputs field.** The spec (Part 5 `PolicyInputs`) has `user_addressed_agent: bool` as the field that captures whether the user has addressed the agent. The spec does not define a separate "deictic_reference"-style field specifically for turn handoffs; `deictic_reference` is for visual deictic gestures (pointing). Using `user_addressed_agent` for "what do you think?" is the correct and only available field.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed in this PR. PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched. Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms gate unmeasured): Not touched. Still active.

---

### Cross-PR watch-items created

None. Both blockers are fully contained within this test file and fixture.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #12 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 12
**Verdict:** APPROVE

**Task 11 ordering resolution:** Task 11 (`test_direct_question_latency`) is not skipped — it requires a real ForegroundModel on GPU and b200 model deployment is actively in progress. Project lead has explicitly authorized parallel progress on Task 12 (a local fixture-driven test with no dependency on Task 11). BLOCKER 1 from round 1 is resolved. Ledger note: Task 11 in flight — b200 model deployment underway.

**Fixes verified:**

- [BLOCKER 2 — RESOLVED] Contrast case label/comment fixed. `case.json` `contrast_trace[0].notes` now reads "EOU confirmed, social_mode stays user_addressing_agent, but user_addressed_agent=False — tests gate-4 discriminator." Test comment at lines 65–67 matches. Docstring at line 41 matches. All accurate.

- [SHOULD-FIX 1 — RESOLVED] `fixture_conventions` key added to `case.json`. `contrast_trace` entry accurately describes it as a fixture-internal negative example not part of the standard EvaluationCase schema and not injected into the event stream.

- [SHOULD-FIX 2 — RESOLVED] `fixture_conventions.expected_events_markers` documents that `eou_confirmed` and `full_response_decision` in `expected_events` are fixture-internal expectation markers, not real event-stream event_types emitted by any adapter.

- [NIT — CONFIRMED CLEAN] No dead `signal_trace[0]` read exists in the file. The NIT was a false positive in round 1. Current file has only `fixture["signal_trace"][1]` (line 47). Confirmed.

**New findings:** none.

**Round-1 PASS items still intact:** local-vs-b200 determination correct; contrast case non-vacuous; spec fidelity of ReasonCodes; `caused_by` threading asserted; adapter purity; scope limited to test file and fixture only.

**Scope check:** Only `tests/test_explicit_turn_handoff.py` and `companion_harness/fixtures/explicit_turn_handoff_001/case.json` changed. No scope creep introduced in fix commits.

**Watch-item checks:** 3, 8, 9, 12, 15, 16 — all unchanged, still active as prior.

**Cross-PR watch-items created:** none.

**PR #12 converged.**

---

## PR #13 — Implement and pass test_false_interruption_rate (ROADMAP Task 13)  (reviewed 2026-05-14)
**ROADMAP task:** 13
**Verdict:** REQUEST-CHANGES

**Summary:** The test is vacuous by construction — the same gate-fires-first failure mode that sank `test_thinking_pause` (PR #9). Every fixture frame has `user_speaking=True`, which causes `speak_policy.decide()` to return `silence` at gate 2 before gates 3 (`eou_probability`) or 4 (`user_addressed_agent`) are ever evaluated. The `eou_probability` values (0.05–0.45) and `user_addressed_agent=False` are irrelevant to the outcome. A policy implementation with its `eou_probability` gate deleted entirely — or raised to 0.99 — would still pass this test with zero false interruptions, because `user_speaking=True` makes silence structurally guaranteed regardless of any other logic. The test does not exercise the false-interruption contract.

---

### Vacuousness analysis (tracing decide() against the fixture)

Every frame in `signal_trace` has `user_speaking=True` and `social_mode="user_addressing_agent"`.

`decide()` decision path for every frame:
1. Gate 1: `inputs.social_mode in _BLOCKING_SOCIAL_MODES` — `"user_addressing_agent"` is NOT in `_BLOCKING_SOCIAL_MODES` (`{"user_addressing_other", "group_conversation", "background_presence"}`). Does not fire.
2. Gate 2: `if inputs.user_speaking:` — `True`. FIRES. Returns `_silence(NOT_ADDRESSED_TO_AGENT)`.
3. Gates 3 and 4 are never reached.

The `eou_probability` field (the primary mechanism for false-interruption: responding during a high-EOU mid-utterance pause) is never consulted on any frame. The `user_addressed_agent` field is never consulted. Deleting all code below gate 2 in `decide()` would not change the outcome of a single frame. The test would pass on that broken implementation.

The coder's claimed failure modes:
- "Relaxing the `user_speaking` guard would trip it" — TRUE only if `user_speaking=False` were set on frames, but that's a different scenario (no longer "user speaking"). The test does not prove the `eou_probability` threshold is correct.
- "Lowering the `eou_probability` threshold would trip it" — FALSE. Even if the threshold were lowered from `> 0.5` to `> 0.05`, gate 2 still fires first on every frame and returns silence. The eou gate is never reached.

The `eou_probability` values (max 0.45) create a SECONDARY redundant backstop at gate 3, but only if gate 2 were removed. With gate 2 in place, the secondary backstop is structurally invisible to this test.

**This is exactly the failure mode documented in PR #9 rounds 1–3:** a different gate than the one being tested always fires first, making the assertion trivially satisfiable.

---

### BLOCKER findings

**[BLOCKER] `tests/test_false_interruption_rate.py:64–77` and `companion_harness/fixtures/false_interruption_001/case.json` — The test is vacuous: `user_speaking=True` on all 200 frames forces silence at gate 2, making `full_response` structurally unreachable regardless of whether the false-interruption-relevant logic (gates 3–4) is intact.**

The false-interruption scenario being tested is: the policy must not emit `full_response` when the user is mid-utterance, specifically when a mid-utterance pause causes a brief spike in `eou_probability`. The relevant policy gates for this are:
- Gate 2 (`user_speaking`): the VAD indicates the user is actively speaking — obvious block.
- Gate 3 (`eou_probability <= 0.5`): the EOU threshold — the false-interruption-relevant gate for mid-utterance pauses where VAD goes silent momentarily.

A meaningful false-interruption test must exercise gate 3, not just gate 2. This means including frames where `user_speaking=False` (the VAD has gone silent) but `eou_probability` is still below threshold — i.e., the filler/mid-utterance pause that the policy must correctly treat as non-terminal. If every frame has `user_speaking=True`, the `eou_probability` gate is never reached, and the test proves nothing about false-interruption behavior.

Fix: The fixture must include frames that represent the actual false-interruption risk scenario:
- `user_speaking=False` (VAD has gone quiet — this is where false interruptions actually occur)
- `eou_probability` in the 0.05–0.45 range (below the 0.5 threshold — policy should stay silent)
- Optionally mix in a few frames with `eou_probability > 0.5` but `user_addressed_agent=False` to exercise gate 4 as a secondary suppressor

The existing `user_speaking=True` frames can remain as part of the fixture to represent the unambiguous mid-speech case, but the critical false-interruption scenario requires `user_speaking=False` frames with sub-threshold EOU. With those frames present, the test would genuinely fail if:
- The `eou_probability` threshold in `decide()` were raised to 0.6 (and the filler frames had `eou_probability=0.55`)
- Gate 3 were removed from `decide()` entirely

Note: the `user_addressed_agent=False` on all frames remains a second independent suppressant for gate 4. Once `user_speaking=False` frames are added, the test must verify that silence comes from gate 3 (`eou_probability <= 0.5`), not gate 4. To make the gate 3 check load-bearing, at least some frames should have `user_addressed_agent=True` so that only gate 3 prevents `full_response`.

---

### Scope fidelity check

**Scope: PASS.** PR adds exactly `tests/test_false_interruption_rate.py` and `companion_harness/fixtures/false_interruption_001/case.json`. No adapter modules touched. No model SDK imported (imports are `companion_harness.fixtures.loader`, `companion_harness.schemas`, and `companion_harness.speak_policy` — no external SDK). PASS.

**ROADMAP task ordering: PASS.** Task 13 is the lowest-numbered incomplete task (Tasks 10, 11, 12 have been approved; Task 11 in-flight on b200 per PR #12 round 2 resolution). PASS.

---

### Fixture-shrinkage guards — assessed

- `assert len(signal_trace) >= 100` — correct guard; 200 frames satisfies it. Meaningful: would catch a fixture accidentally truncated below statistical significance. PASS.
- `assert duration_ms >= 599_000` — correct guard; the fixture spans 600,000ms (`signal_trace[-1]["t_ms"] = 600_000`, `signal_trace[0]["t_ms"] = 0`, so `duration_ms = 600_000`). Note: `600_000 >= 599_000` passes. The guard is slightly loose (allows a fixture 1 second shorter than 10 minutes), but this is intentional tolerance and acceptable. PASS.

---

### Per-10-min rate computation

`count < 1` where `count` is a non-negative integer means `count == 0`. The spec gate is `false_interruption_count_per_10_min < 1`. The fixture is exactly 10 minutes. So `count < 1` at exactly 10 minutes of fixture is the correct and precise encoding of the spec gate — there is no ambiguity about whether to normalize by window length. **This is correct and matches the spec gate exactly.** PASS.

---

### Spec fidelity check — v0.1a scope vs v0.1b

The spec defers `test_backchannel_survival` to v0.1b (ROADMAP line 38). This test must NOT be a de facto backchannel discrimination test. The fixture represents fillers/mid-utterance pauses, not backchannels during assistant speech. The scenario (`user_speaking=True`/`user_speaking=False` mid-utterance with low EOU) is the correct scope for v0.1a `test_false_interruption_rate`. The fixture `scenario` field is `"filler_backchannel_stream"` — the `backchannel` part of the name is mildly confusing given v0.1b deferral, but the `fixture_conventions.notes` field correctly limits the scope: "Filler signals are characterised by user_speaking=true OR eou_probability <= 0.45." This is v0.1a-appropriate; the test is not testing backchannel discrimination. CONDITIONAL PASS — the scenario name should be clarified once the blocker is fixed (see NIT).

---

### Non-vacuous assertions check

- `assert fixture["case_id"] == "false_interruption_001"` — identity check; sanity guard. Non-vacuous. PASS.
- `assert fixture["expected_metrics"]["false_interruption_count_per_10_min"] == "<1"` — fixture schema guard. Non-vacuous (would catch a coder who changed the gate string). PASS.
- `assert len(signal_trace) >= 100` — shrinkage guard. Non-vacuous. PASS.
- `assert duration_ms >= 599_000` — duration guard. Non-vacuous. PASS.
- `assert count < 1` — the gate assertion. **Vacuous** for the reasons explained in the BLOCKER above. Every frame returns silence at gate 2 regardless of any policy logic below it.

---

### NIT findings

**[NIT] `companion_harness/fixtures/false_interruption_001/case.json:22` — `scenario: "filler_backchannel_stream"` contains "backchannel" despite the ROADMAP explicitly deferring backchannel discrimination to v0.1b. The fixture represents filler-heavy mid-utterance speech, not backchannels during assistant speech. Rename to `"filler_mid_utterance_stream"` to avoid implying this test exercises backchannel logic.**

**[NIT] `tests/test_false_interruption_rate.py:16` — `_inputs_from_frame` takes `idx: int` as a parameter but never uses it inside the function body. Delete the unused parameter.**

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed in this PR. PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched. Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 gate unmeasured): Not touched. Still active.

---

### Cross-PR watch-items created

None. The blocker is entirely within this test file and fixture.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #13 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 13
**Verdict:** APPROVE — PR #13 CONVERGED

**Round-1 BLOCKER — RESOLVED:**

The round-1 blocker was: all 200 frames had `user_speaking=True`, so gate 2 fired on every frame and gates 3/4 were never evaluated. The test was vacuous by construction.

**Round-2 redesign verified:**

Fixture is now 201 frames (100 gate-3 frames + 101 gate-2 frames + 1 final bookend). The coder's PR description says "200 frames" — the actual count is 201 (the bookend at `t_ms=600000` is an even-indexed gate-2 frame). This is inconsequential: `assert len(signal_trace) >= 100` passes trivially with 201 frames.

**Gate-3 non-vacuity trace — rigorous:**

Odd-indexed frames (100 frames; t_ms = 3000, 9000, 15000, ..., 597000):
- `user_speaking=False` — gate 2 does NOT fire. Confirmed by Python verification: all 100 odd-indexed frames have `user_speaking=False`. PASS.
- `social_mode="user_addressing_agent"` — not in `_BLOCKING_SOCIAL_MODES`. Gate 1 does NOT fire. PASS.
- `eou_probability` range: 0.05–0.49 — all satisfy `<= 0.5`. Gate 3 FIRES and returns `silence(NOT_ADDRESSED_TO_AGENT)`.
- `user_addressed_agent=True` on ALL 100 gate-3 frames (verified programmatically). This means gate 4 is NOT a secondary backstop; if gate 3 were broken, gate 4 would return `full_response` on ALL 100 frames.

**Gate 3 is genuinely load-bearing.** Verified programmatically: running `speak_policy.decide()` against all 201 frames produces `false_interruptions = []` (count=0, test passes). If gate 3 is removed entirely, all 100 gate-3 frames reach gate 4, find `user_addressed_agent=True`, and return `full_response` — count=100, assertion `count < 1` fails. This confirms the test WILL fail if false-interruption logic (gate 3) is broken.

**Coder's sanity-check claim verified:** "if threshold became <= 0.0, all 100 gate-3 frames would return full_response and count=100." Correct. With any threshold below 0.05 (the floor of the gate-3 frame `eou_probability` range), gate 3 never fires on these frames, and gate 4 produces `full_response` for all 100. The claim is accurate and conservative.

**Round-1 NITs — RESOLVED:**

- [NIT 1 — RESOLVED] `_inputs_from_frame` signature is `def _inputs_from_frame(frame: dict) -> PolicyInputs:` — no `idx` parameter. Confirmed.
- [NIT 2 — RESOLVED] `scenario: "filler_mid_utterance_stream"` in `case.json`. Confirmed. "backchannel" is gone from the scenario name.

**Duration and shrinkage guards:**

- `duration_ms = 600_000 - 0 = 600_000`. `600_000 >= 599_000`: PASS.
- `len(signal_trace) = 201 >= 100`: PASS.
- Both guards present and correct at test lines 55–63.

**fixture_conventions accuracy:**

`fixture_conventions.notes` accurately describes the interleaved design: even-t_ms frames (gate-2, VAD active) and odd-t_ms frames (gate-3, VAD quiet, `user_addressed_agent=True`, `eou_probability` 0.05–0.49). The description is accurate and matches the actual fixture data. PASS.

**Scope check:** PR adds exactly `tests/test_false_interruption_rate.py` and `companion_harness/fixtures/false_interruption_001/case.json`. No adapter modules touched, no SDK imported. PASS.

**ROADMAP task ordering:** Task 13 is the lowest-numbered incomplete task (Tasks 10, 11, 12 approved; Task 11 in-flight on b200). PASS.

**Spec gate fidelity:** `assert count < 1` against a 600s fixture is the exact encoding of `false_interruption_count_per_10_min < 1`. Correct.

**New findings:** none.

**Watch-item checks:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- Watch-items 8, 9, 12, 15, 16: Not touched. Still active.

**Cross-PR watch-items — current active list:** unchanged from round 1.
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #14 — Implement and pass test_policy_replay_exact (ROADMAP Task 14)  (reviewed 2026-05-14)
**ROADMAP task:** 14
**Verdict:** APPROVE-WITH-NITS

**Summary:** The test is substantively correct. Both methodology checks (recorded-baseline + run-twice) are non-vacuous and able to fail on genuine regressions. All 7 baseline decisions were independently verified against `speak_policy.decide()` logic — every one is correct. Bypassing the stub `replay.py` is spec-acceptable. One SHOULD-FIX (invented `expected_events` event_type, violating fixture discipline established in PR #8/PR #12) and one NIT (imprecise docstring description of the EOU boundary frames).

---

### Tier B replay methodology — analysis

**Is the test genuinely non-vacuous?**

**Recorded-baseline check (Check 1):**
All 7 baseline decisions were independently traced against the live `speak_policy.decide()` logic in `companion_harness/speak_policy.py`. Every one is correct. No vacuity from wrong baselines (which would make the test pass on a broken policy). If any gate logic changes — threshold shift from `<= 0.5` to `< 0.5`, a new branch, a ReasonCode swap — at least one of the 7 frames will fail the baseline assertion. Non-vacuous. PASS.

**Run-twice check (Check 2):**
`astuple(d1) == astuple(d2)` compares ALL 8 `SpeakDecision` fields recursively (list fields become tuples). The `caused_by` list is identical across runs because it comes from `list([frame["frame_id"]])` — deterministic from fixture data. `decide()` currently has no wall-clock, random, or dict-iteration non-determinism, so the check passes. It is non-vacuous as a regression guard: if a future developer adds `time.time()` or `random.random()` to `decide()`, this check will catch it. PASS.

**Branch coverage:**
5 decision branches claimed; verified:
1. Gate 1 (`_BLOCKING_SOCIAL_MODES`): frame-000. COVERED.
2. Gate 2 (`user_speaking`): frame-001. COVERED.
3. Gate 3 (`eou_probability <= 0.5`): frame-002, frame-003. COVERED.
4. Gate 4 (EOU + addressed → full_response): frame-005, frame-006. COVERED.
5. Gate 5 (EOU + not addressed → silence): frame-004. COVERED.
All 5 branches covered. PASS.

**EOU boundary frames at 0.50 and 0.51:**
frame-003 (`eou_probability=0.50`) tests the exact equality of the `<=` boundary — this goes to silence. frame-004 (`eou_probability=0.51`) tests just above the boundary — passes gate 3, hits gate 5. These two together constitute the complete boundary coverage for the `eou_probability <= 0.5` tie-breaking rule. Correct.

**`replay.py` bypass — acceptable:**
`replay.py` is a stub (7 lines, no behavior). The spec Stage 0 definition for `test_policy_replay_exact` is: "input: recorded signal trace; expected: identical action_type, threshold path, reason_code." Calling `decide()` directly IS the policy-layer replay — it is not bypassing anything that the spec requires as an intermediary. `replay.py` is an abstraction layer for wrapping the full Tier B harness (including Tier A end-to-end replay), not a required intermediary for this specific contract test. Task 14 is NOT blocked on implementing `replay.py`. The direct `decide()` approach is spec-correct, minimal, and appropriate. PASS.

---

### Findings

**[SHOULD-FIX] `companion_harness/fixtures/policy_replay_001/case.json:9` — `expected_events: ["policy_decision"]` invents a non-spec event_type.**

`"policy_decision"` is not defined in Part 5. Part 5 enumerates audio domain event_types for `AudioOutputController` and states the list is "not exhaustive — extend as needed," but any extension should be documented in `fixture_conventions`. PR #12 round 1 flagged exactly this pattern (`"eou_confirmed"` and `"full_response_decision"`) as a SHOULD-FIX, resolved by adding a `fixture_conventions.expected_events_markers` entry. This fixture has `fixture_conventions` for `policy_replay_safe`, `baseline_decisions`, and `frame_coverage` — but no entry for `expected_events`. Fix: either (a) add a `fixture_conventions.expected_events_markers` entry documenting that `"policy_decision"` is a fixture-internal expectation marker, not a real adapter-emitted event_type (matching the PR #12 pattern), or (b) remove `expected_events` entirely since the test does not use it and it serves no contract purpose for a Tier B policy replay test. Option (b) is the minimum correct fix.

**[NIT] `tests/test_policy_replay_exact.py:6` — Docstring says "EOU boundary frames at 0.45 and 0.50" but 0.45 is not a boundary — it is a representative sub-threshold case. The actual boundary frame is at 0.50 (frame-003, where `eou_probability == 0.5` exactly tests the `<=` equality). The boundary pair is 0.50 (silence) and 0.51 (passes gate 3), encoded in frame-003 and frame-004. Reword to: "plus two EOU boundary frames: 0.50 (exact equality → silence) and 0.51 (just above → passes gate 3) to confirm the `<=` tie-break."**

---

### What the test gets right

- **No vacuous assertions.** No `or True`, no `>= 0`, no discarded returns. `matched_baseline` and `matched_run2` are both incremented inside the loop and asserted `== 1.0` (as rates) at the end. The final rate assertions are redundant with the per-frame `assert` statements inside the loop (a single frame failure already terminates the test via `AssertionError`), but they provide a useful summary metric and are not harmful. Not a finding.
- **`astuple` coverage.** All 8 `SpeakDecision` fields compared, including `caused_by`, `allowed_prosody_tags`, `max_duration_ms`. Complete.
- **Fixture validity.** All 7 baseline decisions are correct (independently verified). No subtle vacuity from a baseline that matches a broken policy by accident.
- **Adapter purity.** Imports: `companion_harness.fixtures.loader`, `companion_harness.reason_codes`, `companion_harness.schemas`, `companion_harness.speak_policy`. No model SDK anywhere. PASS.
- **No EventLogger wired.** `decide()` does not emit events and does not require a logger — the test does not construct one. No unnecessary setup, no logger-never-started hazard (cf. PR #9 round 1 CONCERN).
- **Scope.** Exactly two files: `tests/test_policy_replay_exact.py` and `companion_harness/fixtures/policy_replay_001/case.json`. No adapter modules changed. No other test files modified. PASS.
- **ROADMAP task ordering.** Task 14 is the lowest-numbered incomplete task (Tasks 10–13 approved). PASS.
- **CLAUDE.md rule 4(b).** A `pytest.skip` stub is converted to a passing test. PASS.
- **`policy_replay_match_rate = 100%` gate.** Both `baseline_match_rate == 1.0` and `replay_match_rate == 1.0` are asserted. PASS.

---

### Invariant checks

- **Invariant #5 (policy replay deterministic):** The run-twice check directly verifies this invariant for the current implementation and provides a regression guard. PASS.
- **Invariant #8 (silence wins ties):** frame-003 (`eou_probability=0.50 <= 0.5` → silence) directly tests the tie-break rule. Baseline is `silence`. PASS.
- **No model SDK import:** PASS.
- **`fixture_conventions` discipline (from PR #8):** PARTIAL — `baseline_decisions` and `frame_coverage` documented; `expected_events` marker not documented. See SHOULD-FIX above.

---

### Watch-item checks

- **Watch-item 3** (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- **Watch-item 8** (VAD numeric defaults not config-driven): Not touched. Still active.
- **Watch-item 9** (quiet_mode not as privacy_mode string): Not touched. Still active.
- **Watch-item 12** (ThinkerProposalGen ambiguity): Not touched. Still active.
- **Watch-item 15** (test_thinking_pause un-skip at v0.1b): Not touched. Still active.
- **Watch-item 16** (physical_user_speech_onset_to_stop_ms gate unmeasured): Not touched. Still active.

---

### Cross-PR watch-items created

None. Both findings are self-contained within this fixture and test file.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #14 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 14
**Verdict:** APPROVE — PR #14 CONVERGED

**Round-1 fixes verified:**

- [SHOULD-FIX — RESOLVED] `companion_harness/fixtures/policy_replay_001/case.json:fixture_conventions` — `"policy_decision"` key is present. Text: "Fixture-internal expectation marker in expected_events. Not a Part 5 spec-defined adapter-emitted event_type; indicates that this fixture exercises the policy decision path and that each frame's SpeakDecision is verified against baseline_decisions." Language is accurate, unambiguous, and consistent with the PR #8/PR #12 `expected_events_markers` discipline. PASS.

- [NIT — RESOLVED] `tests/test_policy_replay_exact.py:6–10` — The original round-1 misleading phrase ("EOU boundary frames at 0.45 and 0.50") is ABSENT from the file. The current docstring reads: "boundary frame: eou_probability=0.50 (equality → silence; gate 3 is <= 0.5); eou_probability=0.45 is sub-threshold (below the boundary, not a boundary value)." This is accurate end to end. The 0.50 boundary frame is correctly identified; 0.45 is correctly labeled sub-threshold. No remaining text mislabels 0.45 as a boundary value. The clarifying addition does not contradict any prior sentence because the prior sentence was replaced entirely. PASS.

**Docstring accuracy — holistic check:**
Lines 1–19 of `test_policy_replay_exact.py` state the spec reference, methodology, boundary semantics, and both check descriptions. All are accurate:
- Spec references (§Part 6 Stage 0, §Part 8): correct.
- "boundary frame: eou_probability=0.50 (equality → silence; gate 3 is <= 0.5)": correct — `decide()` returns silence for `<= 0.5`.
- "eou_probability=0.45 is sub-threshold (below the boundary, not a boundary value)": correct.
- Check 1 and Check 2 descriptions: unchanged from round 1; both accurate.
No misleading text remains.

**case.json validity:** Valid JSON (confirmed structure — 11 top-level keys, all arrays well-formed, `fixture_conventions` now has 4 entries). PASS.

**Round-1 PASS items — all still intact:**
- Both replay checks non-vacuous (baseline match + run-twice identity): PASS.
- All 7 baseline decisions correct against live `speak_policy.decide()` logic: PASS.
- All 5 decision branches covered: PASS.
- Boundary frames at 0.50 and 0.51 intact in `signal_trace`: PASS.
- `astuple` full-field comparison (8 fields): PASS.
- Adapter purity (no model SDK import): PASS.
- Scope (exactly `tests/test_policy_replay_exact.py` + `companion_harness/fixtures/policy_replay_001/case.json`): PASS.
- ROADMAP task ordering (Task 14 is lowest incomplete): PASS.
- CLAUDE.md rule 4(b) satisfied: pytest.skip converted to passing test: PASS.

**New findings:** none.

**Watch-item checks:**
- Watch-items 3, 8, 9, 12, 15, 16: Not touched. All still active as prior.

**Cross-PR watch-items — current active list:** unchanged.
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #15 — Implement and pass test_direct_question_latency (ROADMAP Task 11)  (reviewed 2026-05-14)
**ROADMAP task:** 11
**Verdict:** REQUEST-CHANGES

**Files changed (vs main merge base):**
- NEW: `companion_harness/foreground_model_minicpm.py` — real SDK-backed DuplexModel
- NEW: `companion_harness/fixtures/direct_question_001/case.json` — fixture
- MODIFIED: `tests/test_direct_question_latency.py` — skip → real test

Note: The worktree snapshot shows `test_false_interruption_rate.py` as a skip stub and `false_interruption_001` fixture absent because the branch was cut from `09691b7` (before PR #13 merged). The 3-way merge diff (`git diff main...origin/task-11-test-direct-question-latency`) confirms only 3 files differ from current main — the regression concern is a worktree artifact only, not a merge hazard. Confirmed by `--name-only` diff check.

---

### Adapter purity — PASS

- `companion_harness/foreground_model.py`: identical to main (diff confirmed identical, no changes). PASS.
- `companion_harness/foreground_model.py` contains no import of `torch`, `transformers`, or any GPU SDK. PASS.
- `companion_harness/foreground_model_minicpm.py` imports `torch` and `transformers` at the top level — correctly isolated to the new b200-only module. PASS.
- `tests/test_direct_question_latency.py`: `torch` reached only via `pytest.importorskip("torch")` — the import is conditional on availability. Under system Python (no torch), the module-level `pytest.importorskip` raises `Skipped` and the test is skipped cleanly at collection time. PASS.
- CLAUDE.md rule: "test files MUST NOT import a model SDK directly" — the test does import from `companion_harness.foreground_model_minicpm` (which contains `torch`), but only after `pytest.importorskip("torch")` succeeds. The SDK import is thus gated by the skip guard. CONDITIONAL PASS — the spirit of the rule (no SDK on local machine) is satisfied.

### DuplexModel Protocol conformance — PASS

`MiniCPMDuplexModel` implements `infer(self, audio_frame: bytes) -> ThinkerProposal | None` at line 66–68 of `foreground_model_minicpm.py`. This matches the single method of the `DuplexModel` Protocol in `foreground_model.py:52`. Since `DuplexModel` is `@runtime_checkable`, `isinstance(MiniCPMDuplexModel(), DuplexModel)` returns `True`. PASS.

Watch-item 11 (ThinkerProposal.caused_by repair) is not triggered here because `infer()` returns `None` unconditionally for the text-path test. The adapter's `__init__` calls `chat()` directly, bypassing `process_frame()` and the repair logic entirely — which is correct for a latency test.

---

### Is warm-up in `__init__` spec-faithful or gaming? — JUDGMENT: LEGITIMATE

The three warm-up calls in `__init__` prime the CUDA/JIT cache before measurement begins. In a live deployment, the model would be loaded and warmed before the first user interaction; the initial JIT-compile latency is a one-time startup cost, not a per-request cost that the latency gate governs. The spec's `direct_question_latency` gate (`p50 < 800ms / p95 < 1500ms`) is defined in the context of per-action latency budgets at Stage 1 — these are steady-state operating targets for a running system, not cold-start measurements.

The Part 6 note "prevents passing by being sluggish" is directed at the system that delays a response by design (e.g., unnecessary sleep, artificially slow policy). It is NOT directed at caching optimizations that would exist in the real running system. A cold-start guard for a test that explicitly instantiates the model fresh every test run would be unrealistic and would not reflect the latency the user actually experiences.

**Verdict: warm-up is spec-faithful.** No finding.

---

### Is `max_new_tokens=8` spec-faithful or gaming? — JUDGMENT: CONCERN, NOT BLOCKER

The fixture contains 20 closed yes/no factual questions ("Is the sky blue?", "What color is grass?", etc.). The correct answer to each is 1–5 tokens. At `max_new_tokens=8`, the model can always produce a grammatically complete answer to these specific questions.

However, `max_new_tokens=8` is hardcoded in `chat()` as the default AND is the value used in both warm-up calls and measurement calls. The spec defines `test_direct_question_latency` in terms of the model "responding to a direct question" — it does not prescribe response length, and the gate is specifically a latency gate, not a quality gate. The point of the test is that the system is responsive, not slow-by-design.

The concern: if someone swapped in a harder fixture — open-ended questions requiring longer responses — `max_new_tokens=8` would produce truncated, semantically incomplete responses that still pass `assert response` (non-empty). The gate would pass but the measurement would not represent real-world response latency. With the current fixture of factual closed questions, 8 tokens is an accurate representation of a complete response. But the choice is fixture-coupled: the test and the token limit must be maintained together, and that coupling is not documented.

Additionally, `assert response` checks only non-empty — it does not verify that the response is semantically complete (i.e., not truncated mid-sentence). With `max_new_tokens=8`, a question like "Does bread contain gluten?" could produce "Yes, bread typically contains" (truncated). The assertion would pass.

**Finding: [CONCERN]** See findings section below.

---

### Non-vacuousness of assertions — PASS

- `assert fixture["case_id"] == "direct_question_001"` — fixture identity guard. Non-vacuous.
- `assert fixture["expected_metrics"]["direct_question_latency_ms_p50"] == "<800"` — gate string check. Non-vacuous.
- `assert fixture["expected_metrics"]["direct_question_latency_ms_p95"] == "<1500"` — gate string check. Non-vacuous.
- `assert response, ...` — checks model returned non-empty text for each prompt. Non-vacuous.
- `assert p50_ms < 800` — real measured latency vs hard gate. Will FAIL on a slow model or misconfigured GPU. Non-vacuous.
- `assert p95_ms < 1500` — real measured latency vs hard gate. Will FAIL similarly. Non-vacuous.
- No `or True`, no `>= 0`, no discarded returns. PASS.

---

### Reported results (n=20, B200 GPU 3): p50=148ms, p95=612ms

Both gates pass with substantial margin. The results are plausible for MiniCPM-o 4.5 on a B200 at `max_new_tokens=8` with bfloat16 + sdpa. Not a fake result concern.

---

### p95 formula with n=20

`p95_idx = min(int(0.95 * 20), 20 - 1) = min(19, 19) = 19` — this is the last (maximum) element of the sorted list. With n=20, the `min()` clamp that was added in PR #11 means p95 equals the maximum. With a 612ms p95 vs the 1500ms gate, the test passes comfortably; but if a future run has even one outlier spike (e.g., a page fault during the 20th trial), that outlier alone determines p95. For stable gate measurement, n should be increased or the formula should explicitly acknowledge that with n=20 the p95 index is the maximum.

This is a NIT (the gate still works correctly; the formula produces a valid—if conservative—p95 estimate). For n=20, index 19 is actually the "100th percentile" not the 95th, so the test is slightly stricter (harder to pass) than the spec requires — not a problem for a passing test, but worth noting.

---

### Dependency capture — SHOULD-FIX

`docs/remote-dev.md` explicitly states: "requirements.txt lives in the repo. It is empty at bootstrap; add dependencies as adapters land, pinned with version + hash." This is the first GPU-touching adapter. The PR adds no `requirements-b200.txt` (or similar) pinning:
- `torch >= 2.11+cu128`
- `transformers >= 4.51`
- `minicpmo-utils` (if a package)

A fresh b200 checkout cannot reproduce this test without guessing what to install. The PR description mentions these versions but they are not in any tracked file. This should be a new `requirements-b200.txt` (or a `requirements.txt` with a b200-section) in the repo.

---

### `init_vision=True` — BLOCKER: docstring mismatch + unused weights loaded

`foreground_model_minicpm.py` docstring (line 1) says the adapter is used "text-only" and the `__init__` uses `init_audio=False, init_tts=False`. But `init_vision=True` (line 46) loads the vision tower weights. For a text-only latency test these weights are unnecessary and add GPU memory pressure. More critically, the module docstring claims "text-only" but the model has its vision pathway initialized — this is a factual misrepresentation of the loaded configuration. If someone reads the docstring and assumes no vision weights are loaded (e.g., for GPU memory planning), they will be wrong.

Fix: either set `init_vision=False` (cleaner, true text-only, faster load, less GPU memory) OR update the docstring to say "text + vision initialized; audio and TTS disabled."

**This is raised as BLOCKER because the docstring actively misleads on the model configuration, and the spec requires accurate documentation of adapter configurations (Part 9 is about implementation config fidelity).**

---

### Fixture conformance — PASS WITH NIT

- `case_id: "direct_question_001"` — matches spec Part 6c manifest entry. PASS.
- `stage: 1` — correct. PASS.
- `scenario: "closed_question"` — matches Part 6c. PASS.
- `modalities: ["audio"]` — correct per Part 6c (the fixture convention note acknowledges the v0.1a text-path implementation). PASS.
- `expected_metrics` has `direct_question_latency_ms_p50: "<800"` and `direct_question_latency_ms_p95: "<1500"`. Matches ROADMAP gates. PASS.
- `fixture_conventions` key present — explains `text_prompts` and `n_trials`. PASS.
- `text_prompts`: 20 closed factual questions. All are genuinely closed/factual (yes/no or single-noun answers). PASS.
- `n_trials: "20"` — the convention key documents the count. The test uses `len(fixture["text_prompts"]) = 20` as its trial count. Consistent. PASS.

[NIT] The Part 6c manifest entry for `direct_question_001` specifies only `expected_metrics: {direct_question_latency_ms_p50: "<800"}` (p50 only). The fixture correctly also includes the p95 gate — but that extension is undocumented as an extension. Given that ROADMAP and Part 8 both list the p95 gate explicitly, this is a correct and necessary addition. No finding beyond noting the fixture extends Part 6c (consistent with the pattern established in prior fixtures).

---

### Scope check — PASS (with worktree clarification)

Actual merge diff (confirmed via `git diff main...origin/task-11-test-direct-question-latency --name-only`): 3 files only:
- `companion_harness/fixtures/direct_question_001/case.json` (new)
- `companion_harness/foreground_model_minicpm.py` (new)
- `tests/test_direct_question_latency.py` (modified: skip → real test)

No other adapter modules modified. No adapter logic changed. No other test files touched (from main's perspective). PASS.

---

### ROADMAP task ordering — PASS

Task 11 is the lowest-numbered incomplete task (Tasks 10, 12, 13, 14 are all approved-and-merged per the ledger). PASS.

---

### Invariant checks

- Invariant #1 (no unlogged behavior): `test_direct_question_latency` calls `model.chat()` directly, bypassing `ForegroundModel.process_frame()` and its event logging. This is intentional — the test measures raw model latency outside the full harness wiring. This is not a violation in a unit/contract test context; the invariant applies to the live system path, not to isolated latency benchmarks. CONDITIONAL PASS.
- Invariant #10 (EventLogger non-blocking): Not involved in this test. N/A.
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- Watch-item 8 (VAD numeric defaults): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched. Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 unmeasured): Not touched. Still active.

---

### Findings

- [BLOCKER] `companion_harness/foreground_model_minicpm.py:1,46` — Module docstring says "text-only" (line 1) but `init_vision=True` at line 46 loads vision tower weights. This is a factual misrepresentation. Fix: set `init_vision=False` (true text-only) OR update docstring to "text + vision initialized; audio and TTS disabled."

- [SHOULD-FIX] No `requirements-b200.txt` (or equivalent) added. `docs/remote-dev.md` requires "add dependencies as adapters land, pinned with version + hash." This is the first GPU-touching adapter. Add a `requirements-b200.txt` pinning at minimum: `torch` (version), `transformers` (version), and any `minicpmo-utils` package used. A fresh b200 checkout cannot reproduce the test without this file.

- [CONCERN] `companion_harness/foreground_model_minicpm.py:55` — `max_new_tokens=8` default is tightly coupled to the fixture's yes/no question type. The coupling is not documented in the module or the fixture. If the fixture is ever extended to include open-ended questions (which `direct_question_001` by spec could legitimately include), `max_new_tokens=8` would produce truncated responses that still pass `assert response`. Add an inline comment explaining why 8 tokens is sufficient for the current fixture (closed yes/no questions answerable in ≤5 tokens), and note that the value must be revisited if the fixture changes.

- [CONCERN] `tests/test_direct_question_latency.py:45` — `assert response` checks only that the response is non-empty. It does not check that the response is semantically complete (not truncated mid-sentence by the `max_new_tokens=8` limit). For yes/no closed questions this is fine in practice but is a latent false-pass risk if question types change. Consider `assert len(response.split()) >= 1 and response[-1] in ".!?"` or similar minimal completeness check, or document why non-empty suffices for this fixture.

- [NIT] `tests/test_direct_question_latency.py:50` — With n=20, `p95_idx = min(19, 19) = 19` — this is the maximum element, not the 95th percentile in the standard sense. With the current gate margin (612ms vs 1500ms), this is inconsequential, but the formula produces `p95 == max` whenever n is a multiple of 20. The `n_trials: "20"` fixture convention key should note this. Not a correctness bug (the gate is valid); log it for future awareness.

---

### Cross-PR watch-items created

None. All findings are self-contained within the two new files.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #16 — Implement and pass test_decision_provenance (ROADMAP Task 15)  (reviewed 2026-05-14)
**ROADMAP task:** 15
**Verdict:** REQUEST-CHANGES

**Summary:** The test itself is substantively correct and non-vacuous — the provenance contract is genuinely verified. However, PR #15 (Task 11) has an open BLOCKER from its round-1 review (`init_vision=True` vs "text-only" docstring; missing `requirements-b200.txt`) and has not converged. Submitting Task 15 before Task 11 converges violates CLAUDE.md ordering rule. Additionally, the `rate == 1.0` assertion at lines 91–95 is logically redundant (the preceding `assert not missing_cause` already catches every case that could make `rate < 1.0`), which is a brevity concern. One concern about synthetic `caused_by` sentinel strings not being documented.

---

### ROADMAP task ordering — BLOCKER

**[BLOCKER] CLAUDE.md: "Pick the lowest-numbered task that isn't done. Do that one. Stop."**

PR #15 (ROADMAP Task 11, `test_direct_question_latency`) received REQUEST-CHANGES at round 1 with two open blockers:
1. `companion_harness/foreground_model_minicpm.py:1,46` — docstring claims "text-only" but `init_vision=True` loads vision tower weights (factual misrepresentation).
2. Missing `requirements-b200.txt` pinning GPU dependencies.

No round-2 entry exists in the ledger for PR #15. Task 11 has not converged. PR #16 (Task 15) must not be merged until PR #15 is approved. Same rule; same consequence as PR #12 round 1.

Note: if the project lead explicitly authorizes parallel progress on Task 15 (as was done for Task 12 in PR #12 round 2 re-review), this blocker is resolved. No such authorization is recorded in the ledger.

---

### Event-name reconciliation — CORRECT

The coder claims: `assistant_audio_start` (spec Part 6 Stage 0 prose) == `assistant_generation_start` (Part 5 event_type table + controller + barge_in_001 fixture).

**Verdict: CORRECT.**

Evidence:
- Spec Part 5 table (lines 300–306) lists `assistant_generation_start` as the canonical event_type introduced by `AudioOutputController`.
- Spec Part 6 Stage 0 prose (line 336) says "every `assistant_audio_start` event" — this is shorthand in descriptive prose; the normative event_type table uses `assistant_generation_start`.
- `AudioOutputController._emit("assistant_generation_start", ...)` at `companion_harness/audio_output_controller.py:59` — confirmed. The controller emits the Part 5 name.
- PR #4 round-1 review explicitly confirmed all six controller event_type strings match the canonical Part 5 names. `assistant_generation_start` was verified PASS.
- The two names refer to the same event. The test targets the right event.

These are NOT different events. `assistant_audio_start` is not a separate "playback begins" event — the spec prose uses it as a shorthand, and Part 5 is the normative definition. No blocker here.

---

### Does the test genuinely verify the provenance contract?

**Yes — the test is non-vacuous.**

Trace the test execution path:

1. `await logger.start()` (line 49) — drain loop IS running. Events WILL be collected in `received`. Contrast: PR #9 round 1 CONCERN was that logger was never started. This PR correctly starts it. PASS.
2. For each of 3 utterances: `controller.start_generation(caused_by=["policy-decision-utterance-N"])` calls `_emit("assistant_generation_start", ["policy-decision-utterance-N"])`. The controller creates an Event with `caused_by=["policy-decision-utterance-N"]` (non-empty by caller construction). The event is logged synchronously (`logger.log(evt)` is non-blocking synchronous `put_nowait`).
3. `await logger.stop()` (line 73) — drain loop joins. All queued events are flushed to the sink and land in `received`. PASS.
4. Filter to `_AUDIO_START` type → 3 events. Non-empty. `len >= 1` passes non-vacuously.
5. `not e.caused_by` for each — all are non-empty by controller construction. `missing_cause == []`. `assert not missing_cause` passes.

**Would the test FAIL if the controller stopped populating `caused_by`?**
Yes. If `start_generation` were changed to call `_emit("assistant_generation_start", [])`, then `e.caused_by == []`, `not e.caused_by` is True, `missing_cause` would contain all 3 event_ids, and `assert not missing_cause` would fail. The test catches this regression.

**Is the `len >= 1` guard genuine?**
Yes. If `AudioOutputController.start_generation` were renamed or the `assistant_generation_start` emit line were removed, 3 utterance lifecycles would produce zero `audio_start_events`. `len >= 1` would fail immediately. The guard prevents the empty-set vacuity trap. PASS.

---

### Methodology — is "drive the controller live" acceptable for Task 15?

**Yes — consistent with spec and established project pattern.**

Part 6 Stage 0 `test_decision_provenance` specifies: "every `assistant_audio_start` event has a non-empty caused_by chain." It does not require recorded-fixture replay for this specific test. That prescription is for `test_policy_replay_exact` (Tier B, which uses `recorded signal trace` as input).

Driving `AudioOutputController` directly (without model or fixture) is the same methodology as `test_barge_in` (PR #11), which this review body approved. The test exercises real adapter logic (not a stub or mock), produces real events via the real logger, and asserts the provenance contract on those real events. Spec-acceptable. PASS.

---

### Findings

**[BLOCKER] ROADMAP task ordering** — see above. PR #15 has unresolved blockers. Task 15 must wait for Task 11 to converge (or explicit project-lead authorization of parallel progress).

**[CONCERN] `tests/test_decision_provenance.py:68` — `caused_by=["policy-decision-utterance-1"]` is a synthetic sentinel string, not a real logged event_id from any upstream event in this test.**

The same pattern was raised as CONCERN in PR #11 round 1 (`tests/test_barge_in.py:111–115`) and resolved by correcting the associated comment. Here there is no comment explaining the sentinel nature of these strings. The assertion `assert not missing_cause` verifies structural non-emptiness — it does not verify that the `caused_by` reference resolves to a real logged event (DAG closure). That is acceptable (full DAG closure is Task 16, `test_causal_graph_completeness`), but the comment should say so explicitly, as was done in `test_barge_in.py` after PR #11 round 2.

Fix: add a comment analogous to the barge_in resolution: "# caused_by contains synthetic sentinel event_ids — structural non-emptiness check only; full DAG closure verified in test_causal_graph_completeness."

**[CONCERN — BREVITY] `tests/test_decision_provenance.py:91–95` — `rate == 1.0` assertion is logically redundant.**

`missing_cause = [e.event_id for e in audio_start_events if not e.caused_by]` — if any event has empty `caused_by`, it appears in `missing_cause`.
`assert not missing_cause` — fires immediately if any event has empty `caused_by`.
`with_cause = len(audio_start_events) - len(missing_cause)` — if `missing_cause` is empty (the only way to reach this line), `with_cause == len(audio_start_events)`.
`rate = with_cause / len(audio_start_events)` — always `1.0` at this point.
`assert rate == 1.0` — cannot fail if `assert not missing_cause` already passed.

The `rate` computation and assertion add 5 lines that provide no additional failure surface. The metric name in the error message is the only value. Delete lines 91–95 and add the metric name to the `missing_cause` assertion's error message string instead. This is a CONCERN (not a blocker) because the code is correct — it cannot produce a false pass — just redundant.

---

### Non-vacuity summary — PASS (modulo redundancy)

- `len(audio_start_events) >= 1`: genuine non-vacuity guard. PASS.
- `assert not missing_cause`: the load-bearing provenance assertion. PASS.
- `assert rate == 1.0`: redundant given `assert not missing_cause`, but harmless. See CONCERN above.
- No `or True`, no `>= 0` on a count that can't be negative, no discarded values. PASS.

---

### Async correctness — PASS

- `logger.start()` called before controller use — drain loop running. Events collected.
- `logger.stop()` called after all controller calls — drain loop joined, all events flushed to sink.
- The PR #9 round-1 logger-never-started hazard is absent here. PASS.

---

### Scope check — PASS

- Only `tests/test_decision_provenance.py` is new/modified.
- No adapter modules changed.
- No model SDK imported: imports are `companion_harness.audio_output_controller`, `companion_harness.event_logger`, `companion_harness.schemas` (all stdlib-backed). PASS.
- CLAUDE.md adapter-purity rule: "test files MUST NOT import a model SDK directly." PASS.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- Watch-item 8 (VAD numeric defaults): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched. Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 unmeasured): Not touched. Still active.

---

### Cross-PR watch-items created

None. Both concerns are self-contained within this test file.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #15 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 11
**Verdict:** APPROVE — PR #15 CONVERGED

**Round-1 findings — all 5 addressed:**

- [BLOCKER — RESOLVED] `companion_harness/foreground_model_minicpm.py:1,7,44–52` — `init_vision=False, init_audio=False, init_tts=False` are now all three set at the `from_pretrained` call. The module-level docstring (lines 7–9) now explicitly states "Loads text-only (init_vision=False, init_audio=False, init_tts=False). Vision, audio, and TTS towers are disabled to save GPU memory; the v0.1a test path only exercises text inference." Config and docstring agree. No vision weights loaded. No misrepresentation. PASS.

- [SHOULD-FIX — RESOLVED] `requirements-b200.txt` exists with 12 pinned packages. Header comment correctly identifies the b200-only purpose and includes install instructions distinguishing the PyTorch CUDA index URL from the standard requirements. All real dependencies present: `torch==2.11.0+cu128`, `transformers==4.51.0`, `tokenizers==0.21.4`, `accelerate==1.13.0`, `safetensors==0.7.0`, `huggingface-hub==0.36.2`, `numpy==1.26.4`, `sentencepiece==0.2.1`, `scipy==1.17.1`, `numba==0.65.1`, `minicpmo-utils==1.0.6`, `soundfile==0.13.1`. Pinned from the live b200 venv as of 2026-05-14. PASS.

- [CONCERN 1 — RESOLVED] `companion_harness/foreground_model_minicpm.py:59` — Inline comment present: "8 tokens fits direct_question_001 closed yes/no answers; revisit if fixture gains open-ended questions." The coupling is now documented at the definition site. PASS.

- [CONCERN 2 — RESOLVED] `tests/test_direct_question_latency.py:45` — Inline comment present: "non-emptiness suffices: fixture is closed yes/no; any non-empty reply is complete; this is a latency gate, not a quality gate." The rationale is documented and accurate for the current fixture scope. PASS.

- [NIT — RESOLVED] `tests/test_direct_question_latency.py:50` — Comment present: "at n=20, idx=19 (the max element); gate has ample margin so this is fine." `companion_harness/fixtures/direct_question_001/case.json:fixture_conventions.n_trials` entry documents: "20 — enough for stable p50/p95 without excessive GPU time; at n=20 the p95 index (int(0.95*20)=19) is the max element, so p95 equals max." Math is correct and honestly stated in both locations. PASS.

**Adapter purity — CONFIRMED:**

`companion_harness/foreground_model.py` is identical to main and untouched by this PR (diff confirms zero changes to the file). No `torch`, `transformers`, or GPU SDK anywhere in `foreground_model.py`. `DuplexModel` remains a `typing.Protocol`. PASS.

`foreground_model_minicpm.py` correctly imports `torch` and `transformers` — these are expected and correct in the b200-only module. The test guards the import behind `pytest.importorskip("torch")` and a `torch.cuda.is_available()` skip. Local skip confirmed clean (skips at collection time on system Python). PASS.

**b200 verification confirmed by coder:** `1 passed`, p50=170ms, p95=706ms. Both gates pass with ample margin (p50 < 800ms, p95 < 1500ms).

**New findings:** none.

**Scope check:** PR touches exactly 3 files: `companion_harness/foreground_model_minicpm.py`, `tests/test_direct_question_latency.py`, `companion_harness/fixtures/direct_question_001/case.json`. Plus new `requirements-b200.txt`. No adapter logic changed, no existing test files modified. Clean.

**ROADMAP task ordering:** Task 11 is the lowest-numbered incomplete task. PASS.

**CLAUDE.md rule 4(b):** `pytest.skip` converted to a passing test on b200. PASS.

**Invariant checks (unchanged from round 1):**
- Invariant #2 (no direct Thinker speech): `chat()` returns raw str, not a `SpeakDecision`. PASS.
- Invariant #4 (no proactive speech without policy approval): test bypasses the full harness path intentionally; latency benchmark scope. CONDITIONAL PASS.
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): no SensitiveField constructed. PASS.
- Watch-items 8, 9, 12, 15, 16: not touched. All still active.

**Cross-PR watch-items — current active list:** unchanged from round 1.
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #16 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 15
**Verdict:** APPROVE — PR #16 CONVERGED

**Task-ordering BLOCKER — RESOLVED:**
PR #15 (Task 11, `test_direct_question_latency`) converged in its round-2 re-review (ledger entry immediately above this one). The ordering constraint is fully satisfied. BLOCKER from round 1 is closed.

**CONCERN 1 — RESOLVED (scoping comment):**
`tests/test_decision_provenance.py:68-70` — The comment reads:
"# caused_by here uses synthetic sentinel ids — this test checks structural / non-emptiness of provenance only; full DAG closure is verified in / test_causal_graph_completeness."
Present, accurate, and clearly scoped. Directly mirrors the fix resolution from PR #11 round 2. PASS.

**CONCERN 2 — RESOLVED (redundant rate block deleted):**
`tests/test_decision_provenance.py:86–91` — The `with_cause`/`rate`/`assert rate == 1.0` block is absent. Confirmed: lines 86–91 contain only the `missing_cause` list comprehension and `assert not missing_cause`, with the error message `"assistant_audio_start_with_cause gate violated: events missing caused_by: {missing_cause}"`. The gate label is present in the error message. The sole load-bearing assertion now does the full work: it fails if any `assistant_generation_start` event has empty `caused_by`. PASS.

**Non-vacuity guard — CONFIRMED PRESENT:**
`assert len(audio_start_events) >= 1` at lines 81–84 with a descriptive message explaining that a zero-event trace would produce a vacuous pass. Guard is correct and non-vacuous: a controller that never emits `assistant_generation_start` fails here before the provenance check. PASS.

**Round-1 PASS items — all still intact:**
- Event-name reconciliation (`assistant_audio_start` in spec prose == `assistant_generation_start` in Part 5 table / controller): module docstring lines 6–8 explain the alias; `_AUDIO_START = "assistant_generation_start"` at line 25 is the normative string. PASS.
- Logger started before controller use (`await logger.start()` line 49) and stopped after (`await logger.stop()` line 76): drain loop running; all 3 utterance `assistant_generation_start` events reach `received`. PR #9 logger-never-started hazard is absent. PASS.
- Genuinely exercises the provenance contract: if `start_generation()` were changed to emit `caused_by=[]`, `missing_cause` would contain all 3 event_ids and `assert not missing_cause` would fail. Non-vacuous. PASS.
- Adapter purity: imports are `companion_harness.*` only — no model SDK. PASS.
- Scope: exactly one file (`tests/test_decision_provenance.py`). No adapter modules changed. PASS.

**New findings:** none.

**Watch-item checks:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- Watch-items 8, 9, 12, 15, 16: not touched. All still active.

**Cross-PR watch-items — current active list:** unchanged.
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #17 — Implement and pass test_causal_graph_completeness — recorded fixture leg (ROADMAP Task 16)  (reviewed 2026-05-14)
**ROADMAP task:** 16
**Verdict:** APPROVE-WITH-NITS

**Summary:** The recorded-fixture leg is correctly implemented. The fixture DAG is well-formed. The negative case (`test_orphan_detected_on_missing_predecessor`, present since Task 3) is non-vacuous and sufficient to prove the detector can fail. Watch-item 4 is fully covered. One CONCERN (verbose Event constructor replaceable with `Event(**e)` — 16 lines vs 1). One SHOULD-FIX (`fixture_conventions` key missing for non-spec `expected_events` entries — established discipline since PR #8). No adapter modules modified.

---

### Scope verification

Exact diff vs main:
- `tests/test_causal_graph_completeness.py`: adds `from companion_harness.fixtures.loader import load_fixture` import and `test_causal_graph_completeness_recorded_fixture` function (lines 100–142). No existing test modified.
- `companion_harness/fixtures/causal_graph_001/case.json`: new file, 6-event fixture.
- `companion_harness/causal_graph.py`: **NOT modified.** Confirmed by `git diff`. PASS.
- No model SDK imported anywhere. PASS.
- CLAUDE.md adapter-purity rule satisfied. PASS.

---

### Negative case — is the suite non-vacuous?

`test_orphan_detected_on_missing_predecessor` (line 47, present since Task 3) asserts:
1. `report.orphan_count == 1` (not `>= 0`, not `> 0`)
2. `"evt-2" in report.orphan_event_ids`
3. `report.dangling_refs["evt-2"] == ["evt-missing"]`

A `find_orphans()` that always returns `OrphanReport([], {})` fails all three assertions. The suite cannot pass on a permanently-returning-empty implementation. **Non-vacuous. PASS.**

---

### Watch-item 4 coverage — RESOLVED

Both required conditions verified:

(a) `event_type == "log_drop_or_degrade"` is a valid root, NOT flagged as orphan:
- `causal_graph.py` line 63: `if event.event_type in _ROOT_EVENT_TYPES: continue` — unconditional skip.
- Fixture event `evt-log-drop-001` has `event_type="log_drop_or_degrade"` — skipped by the detector.
- Tested by existing `test_log_drop_or_degrade_is_not_an_orphan` (synthetic) AND by the new `test_causal_graph_completeness_recorded_fixture` (recorded fixture with this exact event). Both pass. PASS.

(b) `_dropped_before_enqueue` sentinel in `caused_by` is NOT flagged as a dangling edge:
- `causal_graph.py` line 17: `_KNOWN_SENTINELS = frozenset({"_dropped_before_enqueue"})` — exempt from unresolvable-ref check.
- Fixture event `evt-log-drop-001` has `caused_by=["_dropped_before_enqueue"]`.
- Tested by existing `test_sentinel_in_non_root_event_type_is_not_an_orphan` (synthetic) AND by the recorded fixture. Both pass. PASS.

**Watch-item 4 is RESOLVED.**

---

### Fixture DAG trace — manual verification

Event 1: `evt-user-speech-001`, `caused_by=[]` — declared root. Not orphan. OK.
Event 2: `evt-vad-signal-001`, `caused_by=["evt-user-speech-001"]` — resolves to event 1 in graph. Not orphan. OK.
Event 3: `evt-policy-decision-001`, `caused_by=["evt-vad-signal-001"]` — resolves to event 2. Not orphan. OK.
Event 4: `evt-generation-start-001`, `caused_by=["evt-policy-decision-001"]` — resolves to event 3. Not orphan. OK.
Event 5: `evt-audio-queued-001`, `caused_by=["evt-generation-start-001"]` — resolves to event 4. Not orphan. OK.
Event 6: `evt-log-drop-001`, `event_type="log_drop_or_degrade"`, `caused_by=["_dropped_before_enqueue"]` — skipped by `_ROOT_EVENT_TYPES`. Not orphan. OK.

All 6 events resolve cleanly. `orphan_count == 0` is the correct expected value. The fixture is well-formed and the test is truthful. PASS.

No duplicate `event_id` values. All 6 IDs are distinct. CausalGraph construction succeeds without ValueError. PASS.

Event schema conformance: all 16 required `Event` fields present in every event object in the fixture. Field types match (`seq_no` is int, `caused_by` is array, `payload_ref` is null). PASS.

---

### Fixture conventions discipline

The `expected_events` list contains `"user_speech_onset"`, `"vad_signal"`, `"policy_decision"` which are not Part 5 canonical event_type strings. Precedent from PR #12 (SHOULD-FIX) and PR #14 (SHOULD-FIX, resolved) is to add a `fixture_conventions.expected_events_markers` entry explaining these are expectation markers, not real adapter-emitted event_types. The fixture has no `fixture_conventions` key at all. The test does not use `expected_events` (it only reads `case_id`, `expected_metrics`, and `events`), so this is purely a documentation/maintainability concern — but the pattern is established and the deviation should be corrected before Task 17 (the ReplayRun report), which may consume `expected_events` lists from all fixtures.

---

### Findings

**[SHOULD-FIX] `companion_harness/fixtures/causal_graph_001/case.json` — Missing `fixture_conventions` key.**

`expected_events` contains `"user_speech_onset"`, `"vad_signal"`, and `"policy_decision"` which are not Part 5 canonical event_type strings emitted by any current adapter. The `fixture_conventions` discipline (established in PR #8, applied in PRs #12, #14) requires a `fixture_conventions.expected_events_markers` entry documenting that these are fixture-internal expectation markers, not real event_types. Task 17 (ReplayRun report) may consume `expected_events` from all fixtures and will encounter an unexplained non-spec string. Fix: add a `fixture_conventions` top-level key with an `expected_events_markers` entry. Minimum text: "user_speech_onset, vad_signal, policy_decision are fixture-internal DAG-node markers, not Part 5 event_types emitted by any adapter. assistant_generation_start and assistant_audio_buffer_queued are canonical Part 5 names. log_drop_or_degrade is the canonical Part 10 backpressure event."

**[CONCERN — BREVITY] `tests/test_causal_graph_completeness.py:112–130` — Verbose `Event(...)` constructor is replaceable with `Event(**e)`.**

The fixture's event dicts have exactly the same 16 keys as `Event`'s required fields (confirmed programmatically). The 19-line list comprehension with per-field `e["field"]` assignment reduces to:
```python
events = [Event(**e) for e in fixture["events"]]
```
CLAUDE.md rule 2: "If 200 lines could be 50, rewrite." This is 19 lines that could be 1. No behavior change; no change to test semantics; `Event` is a `@dataclass` with no `__post_init__` that could reject this. The verbose form also risks silently passing if a field is renamed (the per-field dict access would raise `KeyError`, but so would `**e` with a mismatched key — neither form is safer than the other).

This is a CONCERN rather than a BLOCKER because the code is correct. The `_evt()` helper in the same file already demonstrates that the project style prefers explicit field assignment for constructed-from-scratch objects, but that helper is for synthetic Events built from scratch, not deserialized fixture dicts where `**e` is idiomatically correct.

---

### Non-vacuity check — `test_causal_graph_completeness_recorded_fixture` specifically

- `assert fixture["case_id"] == "causal_graph_001"` — identity guard. Non-vacuous (wrong fixture file fails). PASS.
- `assert fixture["expected_metrics"]["orphan_action_count"] == 0` — gate string check. Non-vacuous. PASS.
- `assert len(events) >= 6` — shrinkage guard. Non-vacuous (truncated fixture fails). PASS.
- `assert report.orphan_count == fixture["expected_metrics"]["orphan_action_count"]` — the gate assertion, comparing against the fixture-encoded expected value (0). Non-vacuous: if any `caused_by` reference in the fixture were changed to an invalid ID that is not a sentinel and not in the graph, `report.orphan_count` would be 1 and this assertion would fail. PASS.

The recorded-fixture test on its own is a positive test only (clean trace → 0 orphans). That is correct and sufficient: the fixture was designed to verify that a well-formed trace passes with 0 orphans. The negative case is covered by the existing `test_orphan_detected_on_missing_predecessor`. The "positive + negative together = full coverage" structure is sound.

---

### ROADMAP task ordering — PASS

All Tasks 10–15 are approved and merged on main. Task 16 is the lowest-numbered incomplete task. PASS.

---

### `orphan_action_count = 0` gate — PASS

The test directly asserts `report.orphan_count == fixture["expected_metrics"]["orphan_action_count"]` where the fixture encodes `0`. This is the exact ROADMAP v0.1a numeric gate. PASS.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. All fixture events have `retention_policy_id="default"`. PASS.
- Watch-item 4: **RESOLVED** — see above.
- Watch-item 8 (VAD numeric defaults): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched. Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 gate unmeasured): Not touched. Still active.

---

### Cross-PR watch-items created

None. Both findings are self-contained within this test file and fixture.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
4. **RESOLVED** — (PR #3 round 1, reconfirmed PR #17) `causal_graph.py` correctly whitelists `log_drop_or_degrade` as a root and treats `_dropped_before_enqueue` as a known sentinel. Both legs tested (synthetic + recorded fixture).
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #17 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 16
**Verdict:** APPROVE — PR #17 CONVERGED

**Round-1 SHOULD-FIX — RESOLVED:**

`companion_harness/fixtures/causal_graph_001/case.json:fixture_conventions` is present and accurate. The `expected_events_markers` entry identifies `"user_speech_onset"`, `"vad_signal"`, and `"policy_decision"` as fixture-internal DAG-node markers (not Part 5 canonical event_types), and correctly names `"assistant_generation_start"`, `"assistant_audio_buffer_queued"`, and `"log_drop_or_degrade"` as real event_type values also present in the events array. The text is unambiguous, accurate, and consistent with the discipline established in PRs #12 and #14. PASS.

The entry is presented as a single prose value (not a nested object), matching the style used in `explicit_turn_handoff_001/case.json` and `policy_replay_001/case.json`. Style is consistent with prior fixtures. PASS.

case.json is valid JSON (confirmed `json.load` succeeds). PASS.

**Round-1 CONCERN — RESOLVED:**

`tests/test_causal_graph_completeness.py:112` — The 19-line verbose `Event(...)` field-by-field construction is replaced with `events = [Event(**e) for e in fixture["events"]]` (1 line). Correctness confirmed: fixture event dict keys are exactly the 16 `Event` dataclass field names — `Event(**e)` succeeds for all 6 fixture events with no TypeError or missing-field error. Programmatically verified: `Event` has 15 named fields (`caused_by`, `event_id`, `event_type`, `payload_hash`, `payload_kind`, `payload_ref`, `retention_policy_id`, `schema_version`, `sensitivity`, `seq_no`, `session_id`, `source`, `subject_class`, `timestamp_mono_ms`, `timestamp_wall`) — exact match with fixture dict keys. PASS.

The `_evt()` helper and all synthetic `Event` constructions elsewhere in the test file are unchanged. No scope creep into the synthetic-test helpers. PASS.

**All 7 tests pass — confirmed:**

```
7 passed in 0.01s
```

- `test_causal_graph_completeness`: PASS
- `test_orphan_detected_on_missing_predecessor`: PASS (negative case, non-vacuous)
- `test_log_drop_or_degrade_is_not_an_orphan`: PASS
- `test_sentinel_in_non_root_event_type_is_not_an_orphan`: PASS
- `test_empty_trace_has_no_orphans`: PASS
- `test_duplicate_event_id_raises`: PASS
- `test_causal_graph_completeness_recorded_fixture`: PASS

**Round-1 PASS items — all still intact:**

- Negative case (`test_orphan_detected_on_missing_predecessor`) non-vacuous: all three assertions would fail on a permanently-returning-empty implementation. PASS.
- Watch-item 4 coverage (log_drop_or_degrade root + _dropped_before_enqueue sentinel): both conditions exercised by synthetic AND recorded-fixture tests. PASS.
- Fixture DAG well-formed with no hidden orphan: all 6 event caused_by chains resolve cleanly; `orphan_count == 0` confirmed programmatically. PASS.
- Scope clean: only `tests/test_causal_graph_completeness.py` and `companion_harness/fixtures/causal_graph_001/case.json` touched; no adapter modules, no other test files. PASS.
- Adapter purity: no model SDK imported. PASS.
- `orphan_action_count = 0` ROADMAP numeric gate directly asserted. PASS.

**New findings:** none.

**Watch-item 4 — confirmed RESOLVED:** Both synthetic-test coverage and recorded-fixture coverage in place. Status unchanged from round 1.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
4. RESOLVED (PR #3 round 1, reconfirmed PR #17 rounds 1 and 2).
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #18 — Task 17 — v0.1a ReplayRun report  (reviewed 2026-05-14)
**ROADMAP task:** 17
**Verdict:** REQUEST-CHANGES

**Deliverable-determination:** The coder determined Task 17's deliverable is a report-generation script (`scripts/v0_1a_replay_report.py`) rather than an implementation of `replay.py`. ROADMAP Task 17 wording is: "First v0.1a ReplayRun report. All 8 tests green; all 9 numeric gates met. Tag the repo v0.1a." The success criterion ("All 8 tests green; all 9 numeric gates met") describes the precondition, not the deliverable artifact shape. A report script that runs the suite and emits a structured gate-status record is a legitimate interpretation of "First v0.1a ReplayRun report." `replay.py` is a stub for the full Tier A/B harness (not required for a milestone report); the report script is the correct artifact for Task 17. Deliverable-determination is CORRECT.

**Note on issue #10 deferral:** One test (`test_thinking_pause`) and one gate (`thinking_pause_false_positive_rate`) were deferred to v0.1b by project lead decision via issue #10. One gate (`physical_user_speech_onset_to_stop_ms_p95`) was documented as unmeasured by watch-item 16. These two facts modify the Task 17 success criterion — the correct v0.1a outcome is 7 MET / 1 DEFERRED / 1 NOT_MEASURED, not 9/9.

**Scope check:** PR adds exactly one file: `scripts/v0_1a_replay_report.py`. `replay.py` is untouched (still the 7-line stub). No adapter modules, no contract tests, no fixtures modified. Clean. `test_thinking_pause.py` is untouched (pytest.skip, per PR #9 round 3). PASS.

---

### Findings

**[BLOCKER] `scripts/v0_1a_replay_report.py:_ADDITIONAL_TESTS` — `test_explicit_turn_handoff` is misclassified as a non-required "additional test."**

ROADMAP §"Required contract tests" lists `test_explicit_turn_handoff` explicitly alongside `test_barge_in`, `test_false_interruption_rate`, `test_direct_question_latency`, `test_policy_replay_exact`, `test_decision_provenance`, and `test_causal_graph_completeness`. It is one of the 8 required contract tests, with `test_thinking_pause` being the 8th (deferred). The script segregates `test_explicit_turn_handoff` into `_ADDITIONAL_TESTS` with status `"PASS"` and labels it "(non-gated)." This misrepresents the ROADMAP: `test_explicit_turn_handoff` is a required contract test, not an optional extra. The report as written implies 6 tests are required (the 6 that map to numeric gates) and 1 is a bonus — this is incorrect and dishonest about the milestone scope. Fix: move `test_explicit_turn_handoff` into the gate table (or a separate required-tests section) with an accurate status. It maps to no numeric acceptance gate but is a required contract test that maps to the ROADMAP criterion "companion responds promptly to 'what do you think?'" The report should say 7 of 8 required contract tests green (1 deferred), plus 1 non-numeric required contract test (`test_explicit_turn_handoff`) green — not hide it in an "additional" bucket. The gate verdict line ("7 of 9 gates met") is specifically about the 9 numeric gates; that count is correct. The issue is that the test classification misrepresents which tests are required.

**[CONCERN] `scripts/v0_1a_replay_report.py:_build_replay_run` — emitted JSON deviates from `ReplayRun` schema.**

`schemas.py:ReplayRun` has 8 fields: `run_id`, `case_id`, `implementation_config_version`, `policy_version`, `started_at`, `finished_at`, `results: dict`, `failures: list[dict]`. The script emits a 9-field dict adding `pytest_summary` (not in schema), and puts the entire gate table in `results` (the spec describes `results: dict` as "metric_name -> measured value" — a flat map). The script is explicit that it is not producing a `ReplayRun` dataclass instance (it builds a raw dict). But the PR claims "builds a ReplayRun-shaped JSON record" — this is only approximately true. Not a behavior bug today (the emitted JSON is useful as a milestone artifact regardless of exact schema conformance), but the discrepancy should be documented. Add a comment explaining that the script emits a superset of `ReplayRun` fields adapted for milestone reporting.

**[NIT] `scripts/v0_1a_replay_report.py:_run_pytest` (line ~175) — `import re` inside a for-loop body.**

`import re` appears inside the `for line in output.splitlines():` loop. Python module-import caching means this is not a functional problem, but `import re` belongs at the top of the file with the other stdlib imports. Move it.

---

### Gate accuracy verification

Each of the 9 gates cross-checked against the ledger:

| Gate | Claimed status | Ledger basis | Accurate? |
|---|---|---|---|
| `policy_replay_match_rate` = 100% | MET | PR #14 round 2 — baseline_match_rate == 1.0 and replay_match_rate == 1.0 asserted | PASS |
| `orphan_action_count` = 0 | MET | PR #17 round 2 — recorded-fixture leg asserts report.orphan_count == 0 | PASS |
| `assistant_audio_start_with_cause` = 100% | MET | PR #16 round 2 — 3 lifecycles, assert not missing_cause | PASS |
| `thinking_pause_false_positive_rate` = 0 | DEFERRED | PR #9 round 3 — pytest.skip per issue #10 | PASS |
| `direct_question_latency_p50` < 800ms | MET, ~170ms | PR #15 round 2 — "p50=170ms" | PASS (values match ledger) |
| `direct_question_latency_p95` < 1500ms | MET, ~706ms | PR #15 round 2 — "p95=706ms" | PASS (values match ledger) |
| `vad_detected_user_speech_to_stop_ms_p95` < 200ms | MET, <30ms | PR #11 round 2 — fixture sink ~10ms/chunk, stop sub-chunk | PASS |
| `physical_user_speech_onset_to_stop_ms_p95` < 350ms | NOT_MEASURED | Watch-item 16 — documented in test_barge_in docstring | PASS |
| `false_interruption_count_per_10_min` < 1 | MET, 0 | PR #13 round 2 — 0 full_response in 600s window | PASS |

All 9 gate statuses are accurate. Numeric values cited for p50/p95 latency match the ledger exactly. The report is honest about DEFERRED and NOT_MEASURED.

---

### v0.1a milestone contract-test milestone status

The v0.1a contract-test milestone is substantively complete with the following honest final state:
- 7 of 9 numeric gates: MET
- 1 of 9 gates DEFERRED to v0.1b (`thinking_pause_false_positive_rate` — issue #10)
- 1 of 9 gates NOT_MEASURED (`physical_user_speech_onset_to_stop_ms_p95` — watch-item 16, real-audio integration required)
- 7 of 8 required contract tests green (1 deferred: `test_thinking_pause` — issue #10)
- `test_explicit_turn_handoff` is green but misclassified in this report as non-required (see BLOCKER above)

The repo tag `v0.1a` is NOT applied by this script (correctly deferred to project lead per ROADMAP Task 17 wording). The tag should be applied after this PR converges.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched. Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 gate unmeasured): Correctly documented in this report as NOT_MEASURED with accurate attribution. Status remains ACTIVE (not closed) — closing requires actual measurement.

---

### Cross-PR watch-items created

None. All findings are self-contained within `scripts/v0_1a_replay_report.py`.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` docstring and in this report as NOT_MEASURED. Remains open until real-audio integration measurement is complete.

---

## PR #18 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 17
**Verdict:** APPROVE — PR #18 CONVERGED. v0.1a contract-test milestone complete.

**All 3 round-1 findings — verified resolved:**

- [BLOCKER — RESOLVED] `_ADDITIONAL_TESTS` renamed to `_REQUIRED_NON_GATED_TESTS`. All three references updated consistently: variable declaration at line 148, `_build_replay_run` reference at line 219, `_gate_summary` iteration at line 265. Section header now reads "Required contract tests (no numeric gate):" (line 264). The entry for `test_explicit_turn_handoff` carries an explicit note: "required by ROADMAP §'Required contract tests'." The comment at line 146 retains the historical "(not in _ADDITIONAL_TESTS)" as accurate explanatory context — not a stale reference. No remaining use of the old name as a live variable. PASS.

- [CONCERN — RESOLVED] `_build_replay_run` (lines 186–191) contains a comment block explicitly documenting the deliberate ReplayRun-INSPIRED divergences: `pytest_summary` is extra (not in `schemas.ReplayRun`), and `results` is a nested object rather than the flat metric_name→value dict the spec describes. The comment directly addresses the round-1 concern and is accurate. PASS.

- [NIT — RESOLVED] `import re` is at the module top level (line 23), alongside `argparse`, `json`, `subprocess`, `sys`, `uuid`. Not inside any loop or function. PASS.

**Gate accuracy — re-confirmed unchanged:**

All 9 gate statuses and numeric values are identical to round 1, verified against the ledger:
- 7 MET (policy_replay_match_rate, orphan_action_count, assistant_audio_start_with_cause, direct_question_latency_p50, direct_question_latency_p95, vad_detected_user_speech_to_stop_ms_p95, false_interruption_count_per_10_min): PASS.
- 1 DEFERRED (thinking_pause_false_positive_rate — issue #10): PASS.
- 1 NOT_MEASURED (physical_user_speech_onset_to_stop_ms_p95 — watch-item 16): PASS.
The verdict line "7 of 9 gates MET" at lines 272–276 is correct and unchanged. Report is honest.

**`test_explicit_turn_handoff` classification — verified correct:**

The test now appears under "Required contract tests (no numeric gate)" with `status: "PASS"` and a note accurately stating it is "required by ROADMAP §'Required contract tests'" with no numeric gate. This is the correct representation: it is not hidden as a bonus test and is not falsely assigned a numeric gate it doesn't have. PASS.

**Round-1 PASS items — all intact:**

- Correct deliverable determination (report script, not `replay.py`): PASS.
- ReplayRun-shaped JSON artifact with documented divergences: PASS.
- Scope clean — only `scripts/v0_1a_replay_report.py` touched; no adapters, no tests, no `replay.py`, no `v0.1a` tag applied: PASS.
- Report is honest: non-met gates clearly labeled DEFERRED/NOT_MEASURED, not silently claimed as passed: PASS.
- CLAUDE.md rule 4 (one-PR-one-outcome): report script is a documentation artifact for the milestone, consistent with option (c): PASS.

**New findings:** none.

**v0.1a contract-test milestone — FINAL STATUS:**
- 7 of 9 numeric gates: MET.
- 1 gate DEFERRED to v0.1b (`thinking_pause_false_positive_rate` — issue #10).
- 1 gate NOT_MEASURED (`physical_user_speech_onset_to_stop_ms_p95` — watch-item 16).
- All required contract tests either green or explicitly deferred with documented reason.
- `test_explicit_turn_handoff` correctly classified as ROADMAP-required non-gated test: green.
- Repo tag `v0.1a` is a separate step by the project lead (not applied by this script).

**Watch-item checks:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- Watch-items 8, 9, 12, 15: Not touched. Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 unmeasured): Correctly shown as NOT_MEASURED in the report. Remains ACTIVE until real-audio measurement.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Remains open until real-audio integration measurement.

---

## PR #35 — v0.1b Task 13: revert detector_id from DecisionTrace + add test_detector_ablation  (reviewed 2026-05-14)
**ROADMAP task:** v0.1b Task 13
**Verdict:** CHANGES-REQUESTED

**Summary:** The schema revert is correct and clean. The test is substantively non-vacuous — both detector paths use persistent instances, accumulate state across frames correctly, and the attribution assertions are genuinely load-bearing. However, three stale `detector_id` references remain in `companion_harness/fixtures/detector_ablation_001/case.json` after the revert (the fixture was authored when `detector_id` still existed on `DecisionTrace`). These dangling references must be corrected before merge.

---

### Schema revert — `companion_harness/schemas.py`

`detector_id: str = ""` at `DecisionTrace` line 75 is removed. This is the correct revert of the non-spec field introduced in PR #21 (commit `c63826a`). Verified:
- The spec (Part 5 ~line 186 `DecisionTrace`) has no `detector_id` field. PASS.
- `DecisionTrace` is never instantiated at this milestone (policy layer returns `SpeakDecision`; `DecisionTrace` production is wired at Stage 4 Memory). The field had no live callers. PASS.
- The spec-defined attribution mechanism is `TurnSignal.detector: str` (Part 5 ~line 212), which is already present and correctly populated in both `VADDetector` and `SmartTurnDetector`. PASS.
- `roadmap-v0.1b-draft.md` references to `detector_id` are in prose explaining the revert history — these are intentional historical context, not dangling code references. PASS.

---

### Dangling `detector_id` references — `companion_harness/fixtures/detector_ablation_001/case.json`

The fixture was authored in PR #31 (Task 12) while `detector_id` still existed on `DecisionTrace`. PR #35 reverts the field but does not update the fixture prose. Three stale references remain:

**[BLOCKER 1]** `case.json:14` — `"expected_metrics": {"detector_id_attributable": "per_path"}`. The metric key name `detector_id_attributable` uses the reverted term. The actual mechanism under test is `TurnSignal.detector` (spec-defined), not `DecisionTrace.detector_id` (reverted). Rename to `"detector_attributable_per_path": "per_path"` or similar to match the spec field name.

**[BLOCKER 2]** `case.json:17` — `description` field reads "…Each path produces a TurnSignal attributable to its detector via detector_id." This is now factually wrong: `detector_id` was reverted; attribution is via `TurnSignal.detector`. Change "via detector_id" to "via TurnSignal.detector".

**[BLOCKER 3]** `case.json:22` — `fixture_conventions.divergence_design` reads "…each TurnSignal.detector is different, which is what Task 13 asserts. The scenario is not one where only one detector fires; the ablation tests that each fired signal carries the correct detector_id." The final phrase "the correct detector_id" should read "the correct TurnSignal.detector value".

These three are all in the same file and require only string edits. They do not affect the test's behavior (the test asserts on `TurnSignal.detector`, not on any `detector_id` field), but they are misleading prose in the authoritative fixture record and constitute exactly the "dangling detector_id references" that the revert must clean up.

---

### Test non-vacuity — PASS

**VAD path (persistent detector, accumulation verified):**
- Speech frames 000–024: `_ScriptedVADModel` returns `p_speech=0.9 >= 0.5` threshold → `_in_speech=True`, `_silence_ms=0`, returns `None`. Correct.
- Frame 025 (first silence): `p_speech=0.1` → `_silence_ms=32ms`. Frames 026–033: accumulates to `32+8×32=288ms`. Frame 034 (the silence candidate): `_silence_ms=320ms >= silence_onset_ms=300ms` → fires `TurnSignal(detector='vad')`.
- The `VADDetector` instance is created ONCE before the loop. State accumulates across frames. This is not the fresh-per-frame mistake that sank PR #9 rounds 1–2. PASS.
- `assert len(vad_signals) == 1`: Non-vacuous — would fail if VAD never fired (broken accumulation) or fired multiple times (broken reset). PASS.
- `assert vad_sig.detector == "vad"`: Non-vacuous — would fail if `VADDetector` mislabeled its signal. PASS.
- `assert vad_t >= 1088`: Non-vacuous — would fail if VAD fired prematurely. PASS.
- `assert vad_sig.p_done > vad_sig.p_continue`: Non-vacuous — would fail if stub values were swapped. PASS.

**SmartTurn path (persistent detector, model-invocation count verified):**
- Speech frames 000–024: `_frame_rms` of amplitude-500 samples = 500.0 ≥ `silence_rms_threshold=100` → `_in_speech=True`, `_silence_ms=0`, returns `None`. Correct.
- Silence frames 025–033: `_frame_rms=0.0 < 100` → accumulates `_silence_ms` to 288ms.
- Frame 034: `_silence_ms=320ms >= silence_onset_ms=300ms`, `_candidate_fired=False` → fires model → `_ScriptedSmartTurnModel` returns `(0.85, 0.10)` → emits `TurnSignal(detector='smart_turn')`.
- `SmartTurnDetector` instance created ONCE before the loop. PASS.
- `assert smart_model.call_count == 1`: Non-vacuous and doubly load-bearing — would fail if model was called more than once (silence-candidate-once rule broken) or zero times (model never invoked, detection broken). PASS.
- `assert smart_sig.detector == "smart_turn"`: Non-vacuous. PASS.
- `assert smart_sig.p_done == candidate_p_done` and `assert smart_sig.p_continue == candidate_p_continue`: Non-vacuous — would fail if the stub's scripted values were ignored. PASS.

**Negative path:** The test asserts exactly 1 signal from each path. If either detector fired 0 signals, the `assert len == 1` would fail. If a detector fired and mislabeled `TurnSignal.detector`, the string assertion would fail. The test would NOT survive a detector that emits signals with wrong labels. PASS.

---

### Adapter purity — PASS

- `test_detector_ablation.py` imports: `struct`, `pytest` (stdlib), `companion_harness.event_logger`, `companion_harness.fixtures.loader`, `companion_harness.schemas`, `companion_harness.turn_detector_smart`, `companion_harness.turn_detector_vad`. No `torch`, `onnxruntime`, `pipecat`, or any model SDK. PASS.
- Stub models (`_ScriptedVADModel`, `_ScriptedSmartTurnModel`) are pure Python with zero SDK dependency. PASS.
- Module-level `assert isinstance(_ScriptedVADModel(), VADModel)` and `assert isinstance(_ScriptedSmartTurnModel([]), SmartTurnModel)` enforce Protocol conformance at import time — correct per adapter-first discipline. PASS.

---

### ROADMAP task ordering — PASS

v0.1b tasks 1–12 are all merged to main (verified from git log): tasks 1–12 correspond to commits through PR #31 / PR #33. Task 13 is the current PR. No tasks skipped. PASS.

---

### Scope check — PASS

PR diff touches exactly `companion_harness/schemas.py` (one-line deletion) and `tests/test_detector_ablation.py` (new file). The fixture `companion_harness/fixtures/detector_ablation_001/case.json` is NOT in the diff — it was authored in PR #31 and is already on main. The stale `detector_id` references in that file must be corrected by this PR (they are dangling references created by the revert).

---

### CONCERN: EventLogger never started

Both `_make_logger()` calls create `EventLogger` instances that are never `.start()`-ed. The drain loop never runs; `received` lists (discarded as `_`) are never populated. This is not a correctness issue for this test (the test does not assert on logged events), but the pattern of constructing a logger and never starting/stopping it is structurally incomplete. A future developer who tries to add event-level assertions to this test (e.g., verifying `vad_frame` events have `caused_by` closes) would get an empty `received` list and a silently failing assertion. This is a CONCERN, not a blocker — the test as written is correct.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No `SensitiveField` constructed in this PR. `VADDetector` and `SmartTurnDetector` use `retention_policy_id="default"` (non-empty). PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): `VADDetector` constructed with explicit `silence_onset_ms=300, frame_duration_ms=32` in the test. Constructor overrides are used correctly. Still active for the config-wiring task.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): Not touched (this is test_detector_ablation, a different test). Still active.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 unmeasured): Not touched. Still active.

---

### Required changes (numbered, actionable)

1. **`companion_harness/fixtures/detector_ablation_001/case.json:14`** — Rename `expected_metrics` key from `"detector_id_attributable"` to `"detector_attributable_per_path"` (or any name that does not reference the reverted `detector_id` field). The value `"per_path"` can remain.

2. **`companion_harness/fixtures/detector_ablation_001/case.json:17`** — In the `description` field, replace "Each path produces a TurnSignal attributable to its detector via detector_id." with "Each path produces a TurnSignal attributable to its detector via TurnSignal.detector."

3. **`companion_harness/fixtures/detector_ablation_001/case.json:22`** — In `fixture_conventions.divergence_design`, replace the final phrase "the ablation tests that each fired signal carries the correct detector_id" with "the ablation tests that each fired signal carries the correct TurnSignal.detector value".

---

### Cross-PR watch-items created

None. Both findings (dangling fixture prose and logger-never-started concern) are self-contained within the fixture file. No new structural hazard introduced.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #35 — re-review round 2  (reviewed 2026-05-14)
**ROADMAP task:** 13
**Verdict:** APPROVE

### Round-1 findings — all resolved

**[BLOCKER 1 — RESOLVED]** `companion_harness/fixtures/detector_ablation_001/case.json:14` — `expected_metrics` key renamed from `"detector_id_attributable"` to `"detector_attributable_per_path"`. Verified on PR branch (`origin/v0.1b-task-13-detector-ablation`, HEAD `ca7e14d`).

**[BLOCKER 2 — RESOLVED]** `case.json:17` — `description` now reads "…attributable to its detector via TurnSignal.detector." The stale "via detector_id" phrase is gone.

**[BLOCKER 3 — RESOLVED]** `case.json:22` — `fixture_conventions.divergence_design` now ends "…the ablation tests that each fired signal carries the correct TurnSignal.detector value." The stale "the correct detector_id" phrase is gone.

### Repo-wide `detector_id` grep — PASS

Grepped the PR worktree (`.claude/worktrees/agent-a39a54ab8950008b5`) for `detector_id` across all `.py`, `.json`, and `.md` files. Zero hits. The `docs/roadmap-v0.1b-draft.md` file with intentional historical prose is not present on the PR branch — it lives only on `main` and is unchanged by this PR. The only `detector_id`-related string on the PR branch is the corrected `"detector_attributable_per_path"` key in `case.json`.

### `schemas.py` revert — PASS

`DecisionTrace.detector_id: str = ""` is absent from the PR branch. Confirmed by grep returning exit code 1 (no match) on `origin/v0.1b-task-13-detector-ablation:companion_harness/schemas.py`.

### `expected_metrics` rename — no test breakage — PASS

No test file references `"detector_id_attributable"` or `"detector_attributable_per_path"` as a literal key. The `test_detector_ablation.py` test asserts on `TurnSignal.detector` directly; it does not read `expected_metrics` from the fixture JSON. Rename is safe and no test relies on the old key name.

### Pytest — PASS

`pytest` executed in the PR worktree: **44 passed, 1 skipped**. Matches the required gate. `test_detector_ablation` is among the passing tests.

### New findings

None. The fix is minimal, surgical, and introduces no new problems.

### Watch-item checks

All active watch-items (3, 8, 9, 12, 15, 16) are unchanged — this PR does not touch any of their relevant paths.

### Cross-PR watch-items created

None.

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Task 17 scope.

---

## PR #36 — v0.1b Task 14: ReplayRun milestone report  (reviewed 2026-05-14)
**ROADMAP task:** v0.1b Task 14
**Verdict:** APPROVE

---

### Format convention — PASS

v0.1a Task 17 shipped `scripts/v0_1a_replay_report.py` — a generator script (no committed report document). PR #36 ships `scripts/v0_1b_replay_report.py` using the identical format. The convention is matched exactly; this is not a deviation. The PR body claim "mirrors the v0.1a report structure exactly" is accurate.

---

### Gate table completeness — PASS

`docs/roadmap-v0.1b-draft.md §v0.1b numeric-gates table` lists 10 gates. All 10 are present in `_GATES` in the script:

| Gate | PR #36 status | Accurate? |
|---|---|---|
| `thinking_pause_false_positive_rate` | MET | PASS — `test_thinking_pause` now passes (v0.1b Task 6); 3 non-vacuity assertions confirmed in PR #28 |
| `backchannel_false_stop_rate` | MET | PASS — `test_backchannel_survival` passes (v0.1b Task 11, PR #33) |
| `policy_replay_match_rate` | MET | PASS — `test_policy_replay_exact` extended for backchannel branch (v0.1b Task 7, PR #29) |
| `orphan_action_count` | MET | PASS — `test_causal_graph_completeness` passes (PR #17) |
| `assistant_audio_start_with_cause` | MET | PASS — `test_decision_provenance` passes (PR #16) |
| `vad_detected_user_speech_to_stop_ms_p95` | MET | PASS — `test_barge_in` passes, p95 < 30ms (PR #11) |
| `physical_user_speech_onset_to_stop_ms_p95` | NOT_MEASURED | PASS — re-homed per roadmap; supersedes watch-item 16 |
| `direct_question_latency_p50` | MET | PASS — ~170ms on b200 (PR #15); test skips locally, honest attribution |
| `direct_question_latency_p95` | MET | PASS — ~706ms on b200 (PR #15); same |
| `false_interruption_count_per_10_min` | MET | PASS — `test_false_interruption_rate` passes (PR #13) |

---

### Honest attribution — PASS

**`physical_user_speech_onset_to_stop_ms_p95`:** Correctly marked NOT_MEASURED with VisionClaw attribution per `docs/roadmap-v0.1b-draft.md §Re-homed gate`. The note accurately states "physical-world quantity; home is docs/visionclaw-adaptation-plan-draft.md." Watch-item 16 (gate unmeasured) is superseded by the roadmap's explicit re-homing decision — not by this report. Watch-item 16 RESOLVED at roadmap level; the report represents this correctly.

**`direct_question_latency_p50/p95`:** Marked MET using b200 measurements carried from v0.1a PR #15 (~170ms / ~706ms). The test is the 1 skip (torch/CUDA — b200 only); the report explicitly states "test skips locally (no torch/CUDA); passes on b200 venv." This is the identical honest treatment used in the v0.1a report for these same gates. No gate is claimed MET without a corresponding measurement from a test that actually ran. PASS.

---

### `test_thinking_pause` — Watch-item 15 RESOLVED

Watch-item 15 required `test_thinking_pause` to be un-skipped and wired through `SmartTurnDetector` when v0.1b lands. The gate table correctly shows `thinking_pause_false_positive_rate` as MET with `test_thinking_pause` as the backing test. The v0.1b Task 6 review (PR #28) confirmed the test passes non-vacuously with 3 load-bearing assertions. Watch-item 15 is RESOLVED.

---

### pytest — PASS

Ran `pytest -q tests/` directly: **44 passed, 1 skipped** (the skip is `test_direct_question_latency` — `pytest.importorskip("torch")` fires on system Python). Zero failures. Matches the PR claim exactly.

---

### No `v0.1b` tag applied — PASS

`git tag --list` output: only `v0.1a` exists. No `v0.1b` tag. The script's verdict block correctly states "Per ROADMAP: tag v0.1b is a separate step by the project lead." PASS.

---

### `_REQUIRED_NON_GATED_TESTS` — PASS

Mirrors the v0.1a `_REQUIRED_NON_GATED_TESTS` pattern. Contains `test_explicit_turn_handoff` (ROADMAP-required non-gated, carried from v0.1a) and `test_detector_ablation` (new in v0.1b Task 13). Both are correctly classified as required tests with no numeric gate. PASS.

---

### Divergence documentation — PASS

`_build_replay_run` (lines 226–230) contains an explicit comment documenting that the emitted dict is "ReplayRun-INSPIRED, not a strict schemas.ReplayRun instance" with two named intentional divergences (`pytest_summary` extra field; `results` is a nested object not a flat metric map). Same pattern established and reviewed in PR #18. PASS.

---

### Scope check — PASS

Diff is exactly one file added (`scripts/v0_1b_replay_report.py`, 342 lines). No adapter modules touched, no contract tests modified, no fixtures modified. CLAUDE.md rule 4 (one-PR-one-outcome): the script is a milestone documentation artifact, consistent with option (c). PASS.

---

### Adapter purity — PASS

`scripts/v0_1b_replay_report.py` imports only stdlib (`argparse`, `json`, `re`, `subprocess`, `sys`, `uuid`, `datetime`). No `companion_harness` module imported (correct — the script invokes pytest as a subprocess, not by importing test modules). No model SDK anywhere. PASS.

---

### Findings

**[NIT] `scripts/v0_1b_replay_report.py:210–221` — `_run_pytest` only parses `failed` and `skipped` counts on lines that contain `" passed"`.** If a run produces only failures with no passes (i.e., the summary line is `"1 failed"` with no `" passed"` substring), `passed`, `failed`, and `skipped` remain 0 in the returned dict, and the report would misrepresent the suite result as 0/0/0 rather than 0/0/1. This is an edge case that cannot occur on a passing milestone (a passing milestone by definition has `passed > 0`), and the v0.1a script has the identical limitation. Not a blocker; the behavior is correct for all expected milestone states. No action required.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed in this script. N/A. PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): Not touched by this PR. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip at v0.1b): **RESOLVED** — `test_thinking_pause` passes non-vacuously in v0.1b; the gate is correctly listed as MET.
- Watch-item 16 (physical_user_speech_onset_to_stop_ms_p95 unmeasured): Superseded by the roadmap's explicit re-homing of this gate to the VisionClaw track. The report correctly marks it NOT_MEASURED with accurate attribution. Watch-item 16 **RESOLVED** at roadmap level.

---

### Cross-PR watch-items created

None. All findings are self-contained within the script.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. RESOLVED — `test_thinking_pause` un-skipped and passing in v0.1b.
16. RESOLVED at roadmap level — `physical_user_speech_onset_to_stop_ms_p95` re-homed to VisionClaw track per `docs/roadmap-v0.1b-draft.md`.

**v0.1b milestone final status:**
- 9 of 10 numeric gates MET.
- 1 gate NOT_MEASURED (physical_user_speech_onset — re-homed, not a milestone failure).
- All v0.1a tests still green.
- 3 new v0.1b contract tests passing: `test_thinking_pause`, `test_backchannel_survival`, `test_detector_ablation`.
- Tag `v0.1b` is a separate project-lead action — correctly NOT applied by this PR.

---

## PR #37 — fix: make test_no_torch_import order-independent via subprocess isolation  (reviewed 2026-05-14)
**ROADMAP task:** bugfix (no ROADMAP task number — test isolation fix)
**Verdict:** APPROVE

---

### Bug confirmed on main

`/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/ -q` on `main` branch (unfixed):
```
FAILED tests/test_foreground_model.py::test_no_torch_import - AssertionError: ...
1 failed, 44 passed, 2 warnings in 21.23s
```
The test fails when any torch-importing test (here `test_direct_question_latency`, which imports torch transitively via `pytest.importorskip`) runs before it in the same process, polluting `sys.modules`. The buggy assertion `assert "torch" not in sys.modules` reads from the process-global registry — order-dependent by construction.

---

### Fix verified on PR branch (`fix-test-no-torch-import-isolation`)

**Command 1 — full suite:**
```
/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/ -q
45 passed, 2 warnings in 21.31s
```
0 failed. PASS.

**Command 2 — order-sensitive ordering (torch-importing test first):**
```
/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_direct_question_latency.py tests/test_foreground_model.py -q
5 passed, 2 warnings in 17.56s
```
0 failed. PASS. This is the exact ordering that previously triggered the failure.

---

### Subprocess check non-vacuousness — CONFIRMED

The subprocess command is:
```python
"import companion_harness.foreground_model, sys; sys.exit('torch' in sys.modules)"
```

`sys.exit(bool)` behavior: `sys.exit(True)` → exit code 1; `sys.exit(False)` → exit code 0. Empirically verified on the canonical venv:
- `/raid/yid042/venvs/companion-harness/bin/python3 -c "import sys; sys.exit(True)"` → exit code 1.
- `/raid/yid042/venvs/companion-harness/bin/python3 -c "import sys; sys.exit(False)"` → exit code 0.

If `companion_harness.foreground_model` transitively imported `torch`, `'torch' in sys.modules` would be `True`, `sys.exit(True)` would fire, `result.returncode` would be 1, and `assert result.returncode == 0` would fail. The check is genuinely load-bearing — it is not vacuous. A `foreground_model.py` that contained `import torch` at module scope would cause this test to fail.

---

### Brevity, style, correctness findings

**Brevity:** No redundant fallbacks, no dead code, no speculative helpers. The diff is 17 lines: 1 added import, 2 deleted lines (the old single-assertion function body), 14 new lines (docstring + subprocess call + assertion). The subprocess block is the minimum required for fresh-process isolation. `capture_output=True` is correct to prevent subprocess output from polluting pytest's terminal. No excess.

**Style:**
- `import subprocess` inserted alphabetically before `import sys` (correct: 'sub' < 'sys' by ASCII). Matches stdlib-first import group convention. PASS.
- Docstring accurately describes the new behavior and the rationale. PASS.
- No scope creep: only `tests/test_foreground_model.py` modified. No adapter modules, no spec, no draft docs.

**Correctness:** One nit-level imprecision: if `companion_harness.foreground_model` were unimportable (e.g., wrong virtualenv, path issue), the subprocess would also exit non-zero due to `ImportError`, and the assertion message "importing foreground_model pulled in torch" would be misleading — it would not distinguish between "torch was pulled in" and "package not importable." Including `result.stderr.decode()` in the assertion message would make failures diagnostic. In a properly configured canonical venv this scenario does not occur, and the test just verified it does not occur. Not raised as a finding.

---

### Adapter-first invariant intent preserved

The test still genuinely verifies that `companion_harness/foreground_model.py` does not transitively import torch. The subprocess starts with a clean `sys.modules`, imports the module, then checks the registry. The prior version was structurally correct in intent but broken in practice by process-global state. The fix restores the correct behavior. PASS.

---

### No v0.1b tag

`git tag --list` returns only `v0.1a`. No `v0.1b` tag applied. PASS.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No `SensitiveField` constructed. N/A. PASS.
- Watch-item 8 (VAD numeric defaults not config-driven): Not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): Not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): Not touched. Still active.
- Watch-item 15: RESOLVED in PR #36.
- Watch-item 16: RESOLVED at roadmap level in PR #36.

---

### Cross-PR watch-items created

None. The fix is self-contained within one test function in one test file.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults (`_SPEECH_THRESHOLD=0.5`, `_SILENCE_ONSET_MS=300`) must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. RESOLVED — `test_thinking_pause` un-skipped and passing in v0.1b (PR #36).
16. RESOLVED at roadmap level — `physical_user_speech_onset_to_stop_ms_p95` re-homed to VisionClaw track (PR #36).


---

## PR #38 — fix: make stale skip note conditional on actual skip count  (reviewed 2026-05-14)
**ROADMAP task:** cosmetic fix to `scripts/v0_1b_replay_report.py`
**Verdict:** APPROVE

### Diff examined

`scripts/v0_1b_replay_report.py` — `_gate_summary()` only. 2 lines removed, 8 lines added. The unconditional `f"  (1 skip = test_direct_question_latency…)"` line is replaced with a `*([note] if report["pytest_summary"]["skipped"] > 0 else [])` spread into the list literal.

### Verification — canonical venv `/raid/yid042/venvs/companion-harness/bin/python3`

**Command 1 — replay report (PR branch script, canonical venv):**
```
  pytest suite: 45 passed, 0 skipped, 0 failed

  Gate detail:
```
Stale `(1 skip = …)` line does NOT appear when skips = 0. PASS.

**Command 2 — pytest suite:**
```
45 passed, 2 warnings in 21.06s
```
0 failed. Script change does not affect the test suite. PASS.

**Command 3 — skip note still renders when skipped > 0 (injected fake pytest summary):**
```
'  pytest suite: 44 passed, 1 skipped, 0 failed'
'  (1 skip = test_direct_question_latency, no torch/CUDA locally — passes on b200, carried from v0.1a)'
```
Note still appears when skips > 0. The fix did not suppress the note unconditionally. PASS.

### Brevity / style / correctness

**Brevity:** Clean. `*([item] if cond else [])` is the idiomatic minimum for a conditional element in a list literal at one call site. No helper, no wrapper, no dead code.

**Style:** Matches surrounding list-of-f-strings idiom. No new imports. Indentation consistent.

**Correctness:** Condition `report["pytest_summary"]["skipped"] > 0` maps directly to the parsed skip count from `_run_pytest` — correct gate. Hardcoded note text ("1 skip = test_direct_question_latency") would be mildly stale if a future run produced 2 skips, but that is a pre-existing limitation outside this PR's scope and not a finding against this change.

**Scope check:** Only `scripts/v0_1b_replay_report.py` touched. Gate definitions, `_run_pytest` parsing, `_build_replay_run`, exit behavior — all unchanged. PASS.

**Invariant checks:** Not applicable (script-only cosmetic fix; no event logging, policy, or schema code modified).

### Cross-PR watch-items created

None.

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.

---

## PR #39 — docs: refresh ROADMAP.md — mark v0.1a/v0.1b complete, point at v0.1c  (reviewed 2026-05-14)
**ROADMAP task:** docs-only (CLAUDE.md rule 4 category (c))
**Verdict:** APPROVE

**Scope check (verification 1):**
`gh pr diff 39 --name-only` returns exactly one file: `ROADMAP.md`. No code, no spec (`docs/architecture-v0.1.md`), no draft docs touched. PASS.

**Accuracy (verification 2):**

- Tags: `git tag` lists `v0.1a` and `v0.1b`. PR claims both as "tagged `v0.1a`" and "tagged `v0.1b`". PASS.
- v0.1b task count: `git log v0.1a..v0.1b --oneline` shows 17 commits; 14 are v0.1b-labeled tasks (per `docs/roadmap-v0.1b-draft.md` §NEXT TASKS tasks 1–14), plus 2 post-milestone fix PRs and 1 parallel Manual-test Task 1. PR says "14 v0.1b tasks merged" — accurate for the task sequence. PASS.
- 45 tests: PR diff states "45 tests green in the canonical venv." Ledger entry for PR #38 (last merged) confirms `45 passed, 0 skipped, 0 failed`. PASS.
- v0.1c draft file: `docs/roadmap-v0.1c-draft.md` exists. Its status line reads "DRAFT — awaiting final project-lead sign-off." PR correctly reflects this. PASS.
- v0.1c pinned success criterion: verbatim copy of `docs/roadmap-v0.1c-draft.md` lines 27–36. Exact match. PASS.
- v0.1c scope delta items (VisionSidecar, DeicticDetector, ForegroundModel Protocol extension, three new ReasonCode members, Stages 0+1 carried, Stage 2 enabled, Open Question 1 on `clarification` wiring): all accurately sourced from `docs/roadmap-v0.1c-draft.md`. PASS.
- 7 new contract tests named: exact match to `docs/roadmap-v0.1c-draft.md` §Contract-test stages. PASS.
- Live-Loop Integration: `docs/milestone-live-loop-integration-draft.md` exists; PR's description (extra-spec, DRAFT, awaiting sign-off, fully parallel to v0.1c, one DuplexModel coordination point) accurately summarizes the file. PASS.
- Later-stages header updated from "2–6" to "3–6": accurate — Stage 2 is now the active v0.1c milestone. PASS.
- Stage 2 line under "Later stages" now reads "Active in v0.1c (see above)": accurate. PASS.

**No invented scope (verification 3):**
Diff removes 48 stale lines, adds 33 refreshed lines. No new task lists invented. v0.1c scope delta is sourced from `docs/roadmap-v0.1c-draft.md`; live-loop section is summarized from `docs/milestone-live-loop-integration-draft.md`. No milestone created by this PR. PASS.

**Consistency (verification 4):**
v0.1b summary is consistent with `docs/roadmap-v0.1b-draft.md` (two detectors, three new tests, stages 0–1). v0.1c success criterion is verbatim from the draft. PASS.

**Frozen-spec untouched (verification 5):**
Only `ROADMAP.md` in the diff. `docs/architecture-v0.1.md` not touched. PASS.

**Findings:**

- [NIT] `ROADMAP.md` v0.1b section — The b200-specific absolute path `/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/ -q` is embedded in a reader-facing roadmap entry. On any machine other than b200 this path does not exist; a reader following the command would get a not-found error. The value was copied verbatim from the approved v0.1b ReplayRun report (PR #36); it is factually accurate for that context but is misleading in a general-audience doc. This does not rise to a blocker or a CHANGES-REQUESTED finding because the doc is an accurate record of where the canonical test run happened, and the CLAUDE.md `docs/remote-dev.md` reference already scopes b200 tooling. Noted for awareness; no action required before merge.

**New cross-PR watch-items:** none.

**Cross-PR watch-items — current active list (unchanged by this doc-only PR):**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b — deferred from PR #9 round 3) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands. Already documented; repeated here for completeness.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.

---

## PR #40 — Foundational DuplexModel Protocol extension  (reviewed 2026-05-14)
**Tracks:** v0.1c Stage 2 (Task 2) + Live-Loop Integration (Task 1)
**Verdict:** APPROVE

**Canonical-venv pytest result:** `48 passed, 2 warnings in 21.45s` (on PR branch). Baseline on `main` before this PR: `45 passed`. Delta: +3 new tests. Exact match with coder's claimed count.

---

### Checklist

**1. Adapter-first (no SDK import):**
`companion_harness/foreground_model.py` imports only: `hashlib`, `time`, `datetime`, `timezone`, `typing` (stdlib), `companion_harness.event_logger`, `companion_harness.schemas`. No `torch`, `transformers`, `MiniCPM`, or any model SDK anywhere in the import chain. Verified via subprocess `"torch" in sys.modules` test (test_no_torch_import — already passing). PASS.

**2. `@runtime_checkable` correctness:**
Both Protocols carry `@runtime_checkable`. Verified empirically:
- `AudioOnlyFake` (has `infer(audio_frame)`, no `video_frame` param, no `infer_stream`): `isinstance(fake, DuplexModel)` = True. `isinstance(fake, StreamingDuplexModel)` = False. PASS.
- `_FakeStreamingModel` (has both `infer` and `infer_stream`): `isinstance(fake, DuplexModel)` = True. `isinstance(fake, StreamingDuplexModel)` = True. PASS.
- The ADR note claim holds: audio-only fakes still satisfy `DuplexModel` after the extension; the separate `StreamingDuplexModel` Protocol prevents false positives on `isinstance`. PASS.

**3. Invariant #1 — process_stream logs every yielded proposal with non-empty caused_by[]:**
- `frame_evt` is emitted once (before model call) with caller's `caused_by`.
- For each proposal yielded by the model: (a) if `proposal.caused_by` is empty, it is repaired to `[frame_evt.event_id]`; (b) a `foreground_proposal` event is emitted with `caused_by=[frame_evt.event_id]`.
- `test_process_stream_proposals_carry_caused_by` is non-vacuous: the second yielded proposal has `caused_by=[]` (will be repaired); removing the repair code would cause the assertion `assert p.caused_by` to fail on that proposal. The test would also fail if `event_types.count("foreground_proposal") == 2` were not satisfied — confirmed by code trace: two proposals yield two loop iterations, each emitting one `foreground_proposal` event. PASS.

**4. No concrete model touched:**
`gh pr diff 40 | grep "^diff --git"` shows exactly two files changed: `companion_harness/foreground_model.py` and `tests/test_foreground_model.py`. `foreground_model_minicpm.py` is absent from the diff. PASS.

**5. Surgical scope — only the two named files, no opportunistic refactor:**
Every changed line traces directly to one of: (a) adding the `video_frame` optional param to `DuplexModel.infer` and the `process_frame` call-site; (b) adding `StreamingDuplexModel` Protocol; (c) adding `ForegroundModel.process_stream()`; (d) adding the ADR module docstring block; (e) the 3 new tests exercising the above. No adjacent code reformatted, no unrelated helpers introduced. PASS.

**6. Non-vacuous new tests:**
- `test_audio_only_fake_satisfies_duplex_model_protocol` — would fail if `isinstance(fake, DuplexModel)` returned False (regression on the video_frame extension) or if `isinstance(fake, StreamingDuplexModel)` returned True (false positive from the separate-Protocol design). Both assertions are live.
- `test_streaming_fake_satisfies_both_protocols` — would fail if either `isinstance` returned the wrong bool. Live assertions.
- `test_process_stream_proposals_carry_caused_by` — would fail if: (i) proposals count != 2, (ii) any proposal has empty `caused_by`, (iii) `foreground_frame` not in log, (iv) `foreground_proposal` count != 2. The second proposal is explicitly seeded with `caused_by=[]` to exercise the repair path. All four assertions are non-vacuous. PASS.

**7. No spec edit:**
`docs/architecture-v0.1.md` not in the diff. PASS.

**8. ADR note present and accurate:**
The `## Protocol structure (ADR note)` block in the module docstring is present (lines ~28–52 of the PR diff). It documents: (a) the optional-parameter strategy for `video_frame` and why it preserves audio-only fake compatibility under `@runtime_checkable`; (b) the separate-Protocol strategy for `StreamingDuplexModel` and why it prevents false `isinstance` failures on audio-only fakes; (c) `process_stream()` logging semantics (invariant #1, `caused_by[]`). The claims are accurate as verified above. PASS.

---

### Findings

**[concern / correctness] `companion_harness/foreground_model.py:138`** — `ForegroundModel.__init__` accepts `model: DuplexModel`, but `process_stream()` requires the injected model to satisfy `StreamingDuplexModel`. No runtime guard exists: calling `process_stream()` with an audio-only `DuplexModel` raises `AttributeError` at the `self._model.infer_stream(...)` line, suppressed at type-check time by `# type: ignore[attr-defined]`. The docstring says "The injected model must satisfy StreamingDuplexModel" — the contract is documented but not enforced and the type annotation of `__init__` does not communicate the distinction. This is not a defensive-shim request (the scenario where a caller passes an audio-only model and calls `process_stream()` IS possible), but it is an undetected contract violation that will produce a confusing `AttributeError` rather than a meaningful `TypeError`. A lighter fix than adding a guard: the `model` parameter annotation on `__init__` could be annotated `DuplexModel` with a note, OR the ADR could call this out explicitly. Per CLAUDE.md rule 2 ("No error handling for impossible scenarios") — this IS a possible scenario, so a guard is appropriate — but given the DRAFT status of both tracks and the stub framing of this PR, elevating to CHANGES-REQUESTED is disproportionate. Flag for the concrete-model PR (Live-Loop Task 2 / v0.1c Task 2 impl) when the real `StreamingDuplexModel` is injected.

**[nit / style] `companion_harness/foreground_model.py` module docstring** — The new `## Protocol structure (ADR note)` section uses `##` (two hashes) while the existing `# SPEC AMBIGUITY` section uses `#` (one hash). Both are inside a Python docstring where `#` characters are plaintext, not parsed Markdown — but the inconsistent heading levels are a minor style nit. Not a behavior issue; not a blocker.

**[nit / brevity] `tests/test_foreground_model.py:235`** — The `_frames()` async generator yields exactly one frame and is defined as a named function used in exactly one test. Could be replaced with an inline async generator expression `(f async for f in [(b"\x00" * 512, None)])` — but the named function reads more clearly. Style preference; not a finding that blocks merge.

---

### Rule 4 analysis

CLAUDE.md rule 4: "each PR ships exactly one of: (a) stub, (b) skip-to-green, (c) docs update. No mixing."

This PR ships: (1) the `video_frame` optional param extension on `DuplexModel.infer` (v0.1c task 2), and (2) `StreamingDuplexModel` + `ForegroundModel.process_stream()` (live-loop task 1). The live-loop integration draft explicitly classifies live-loop task 1 as **"stub PR (a): interface only."** The v0.1c task 2 (optional param) is inseparably coupled to live-loop task 1 in a single file — the live-loop draft and ROADMAP both document the coordination point ("one owns the change, the other consumes it"). Bundling them avoids a merge conflict between two parallel-track PRs and is explicitly contemplated by the ROADMAP language. Both tracks are DRAFT (awaiting project-lead sign-off), and the task-ordering strictness applied in PR #12 (BLOCKER) was against finalized v0.1a milestones. The bundled seam PR is the ROADMAP-specified solution to the coordination constraint. Rule 4 is satisfied under option (a) at the track level for both tasks.

---

### Correctness of Protocol design — key technical verification

`StreamingDuplexModel.infer_stream` is typed `async def ... -> AsyncGenerator[ThinkerProposal, None]` — meaning it is a coroutine that RETURNS an async generator. Callers must `await` the call to get the generator, then `async for` over it. `process_stream()` correctly does `async for proposal in await self._model.infer_stream(...)`. This is the correct Python idiom. If an implementor instead made `infer_stream` an async generator function (using `yield` directly), calling it would return an `async_generator` object and `await` on that would raise `TypeError`. This constraint is implicit in the Protocol signature — the ADR note does not call it out. Consider flagging for the concrete-model PR (Live-Loop Task 2).

`ForegroundModel.process_stream` is itself an async generator function (`yield` inside `async def`). Callers must `async for p in fm.process_stream(...)` — no `await`. The test does this correctly. No mismatch.

---

### Watch-item checks

- Watch-item 3 (SensitiveField.retention_policy_id non-empty): `_emit()` hardcodes `retention_policy_id="default"`. Non-empty. PASS.
- Watch-item 8 (VAD numeric defaults): not touched. Still active.
- Watch-item 9 (quiet_mode not as privacy_mode string): not touched. Still active.
- Watch-item 12 (ThinkerProposalGen ambiguity): not touched. Still active.
- Watch-item 15 (test_thinking_pause un-skip): not touched. Still active.
- Watch-item 16 (physical barge-in gate unmeasured): not touched. Still active.

---

### Cross-PR watch-items created

17. (Live-Loop Task 2 / v0.1c Task 2 impl) When the concrete `StreamingDuplexModel` implementation is written (in `foreground_model_minicpm.py`): (a) verify `infer_stream` is a coroutine that RETURNS an async generator (not an async generator function itself — the two are not interchangeable under the `await ... then async for` call pattern); (b) verify `ForegroundModel.__init__` or the concrete construction site enforces that the injected model satisfies `StreamingDuplexModel` (not just `DuplexModel`) when `process_stream()` will be called.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.
17. (New) Concrete `StreamingDuplexModel` impl must use coroutine-returns-generator pattern for `infer_stream` and the construction site must enforce the `StreamingDuplexModel` contract. Flag on the b200 model implementation PR.


---

## PR #41 — stub DeicticDetector: Protocol + skeleton (v0.1c Task 5)  (reviewed 2026-05-14)
**ROADMAP task:** v0.1c Task 5
**Verdict:** CHANGES-REQUESTED

### Pytest summary (canonical venv)

`/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/ -q` — **65 passed**, 0 failed, 2 warnings in 21.71s. New tests: `tests/test_deictic_detector_stub.py` — 3 passed. No regressions.

(Note: PR description claims "52 passed (49 prior + 3 new)" — the actual baseline before this PR was 62 tests, not 49. The claim is wrong but the tests themselves pass. The body is stale copy from an earlier PR template.)

---

### Findings

**1. BLOCKER — brevity / scope creep**
`companion_harness/deictic_detector.py:71–84` (`classify()`) and `companion_harness/deictic_detector.py:88–123` (`_next_seq()`, `_emit()`)

Task 5 success criterion (roadmap §Task 5): "imports cleanly; isinstance Protocol check passes with a fake `DeicticModel`." Task 6 is "Implement `DeicticDetector`. Invokes the injected `DeicticModel`, classifies deictic vs non-deictic, logs every invocation as an event (invariant #1)." This PR ships Task 6's body (a working `classify()` with full event construction and logging) inside the Task 5 stub PR. CLAUDE.md rule 2 is explicit: "No features beyond what the current ROADMAP task asks for" and "One PR, one outcome." The `classify()` implementation and the entire `_emit()` machinery are Task 6 scope.

Fix: Replace `classify()` with a `raise NotImplementedError` body and delete `_next_seq()` and `_emit()` entirely. The Protocol and class constructor belong here; the implementation does not.

---

**2. BLOCKER — correctness (non-vacuous test claim violated)**
`tests/test_deictic_detector_stub.py:45–57` (`test_classify_returns_model_result`)

The `_make_logger()` helper creates a `received: list[Event]` and returns it, but `EventLogger.log()` enqueues onto an `asyncio.Queue` — the drain task never runs without `await logger.start()`. The `received` list is always empty after `classify()` returns. The test claims to exercise "return plumbing" but the event-logging half of classify() is completely invisible to the test. This is a non-vacuous test that is vacuous in practice: it asserts only the model's return values, not the event emission that the PR description claims it covers.

Additionally, if finding 1 is resolved and `classify()` becomes `raise NotImplementedError`, this test needs to be a `pytest.skip` or removed until Task 6.

Fix (after resolving finding 1): Remove `test_classify_returns_model_result` and the `_make_logger` helper entirely. The Task 5 tests should be exactly two: `test_fake_satisfies_protocol` (required by success criterion) and `test_deictic_detector_construction` (Protocol + injection check). No event-logging test until Task 6 wires an async loop correctly.

---

**3. Concern — brevity / dead annotation**
`companion_harness/deictic_detector.py:92–98` (`_emit()` return type `-> Event`)

`_emit()` is declared `-> Event` but `classify()` on line 83 discards the return value. Every other detector in the codebase that declares `-> Event` on `_emit()` captures the result for downstream causal chaining (`BackchannelClassifier` uses it in `evidence_event_ids`, `SmartTurnDetector` uses it in a chained `caused_by`). Here the returned `Event` goes nowhere. This is either dead annotation (the return type should be `-> None`) or a latent bug (Task 6 needs the event_id for downstream `caused_by` and will have to restructure the call anyway).

This finding becomes moot if finding 1 is resolved and `_emit()` is deleted.

---

### Adapter-first check

PASS. No import of `torch`, `onnxruntime`, `pipecat`, or any model SDK. `DeicticModel` Protocol is the injection seam.

### Style consistency check

The Protocol shape, class constructor, `SOURCE` / `SCHEMA_VERSION` constants, and module docstring ADR note all match the established detector-adapter pattern. The ADR note is accurate and complete.

### Spec fidelity check

`deictic_reference: bool` is present in `PolicyInputs` (`companion_harness/schemas.py:93`). The classifier returns `(bool, float)` and maps cleanly to that field. No invented fields or enums outside the spec. PASS.

### Scope check

Two new files only: `companion_harness/deictic_detector.py` and `tests/test_deictic_detector_stub.py`. No edits to existing files. Surgical. PASS.

---

### Cross-PR watch-items created

18. (v0.1c Task 6 / DeicticDetector impl) When Task 6 is written: (a) `_emit()` return value must be captured and used — either in `evidence_event_ids` on a structured return type, or chained into a follow-on event's `caused_by`. (b) The Task 6 test must use `@pytest.mark.asyncio`, `await logger.start()` / `await logger.stop()`, and assert `len(received) == 1` with the correct `event_type` and `caused_by`, matching the pattern in `test_backchannel_classifier_stub.py:56–106`.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
15. (v0.1b) `test_thinking_pause` must be un-skipped and wired through `SmartTurnDetector` when v0.1b lands.
16. (Active) `physical_user_speech_onset_to_stop_ms_p95 < 350ms` gate unmeasured. Documented in `test_barge_in.py` module docstring as Task 17 scope.
17. (Active) Concrete `StreamingDuplexModel` impl must use coroutine-returns-generator pattern for `infer_stream` and the construction site must enforce the `StreamingDuplexModel` contract. Flag on the b200 model implementation PR.
18. (New) DeicticDetector Task 6: capture `_emit()` return for causal chaining; test must use asyncio harness and assert event emission count, type, and caused_by.

---

## PR #43 — v0.1c Task 4: add Stage 2 ReasonCode members  (reviewed 2026-05-14)
**ROADMAP task:** v0.1c Task 4
**Verdict:** APPROVE

**Canonical-venv pytest result:** `48 passed, 2 warnings` — zero delta over baseline. No new test file required (spec says "enum members import cleanly; no existing tests broken; each new member has a docstring" — static verification only).

**Spec fidelity — PASS:**
Three members added exactly as named in v0.1c Task 4: `DEICTIC_AMBIGUOUS`, `VISUAL_LOW_CONFIDENCE`, `AUDIO_VISUAL_CONFLICT`. Values are self-named strings (consistent with all 10 existing members). No members reordered, no existing member modified. Surgical append only. PASS.

**Docstrings — PASS:**
`__doc__` assignment post-class-definition is a valid Python idiom for enum members (confirmed: all three `__doc__` attributes are set and readable). Content matches the spec verbatim: "cannot be resolved to a single candidate" / "hallucination-resistance threshold" / "contradict." PASS.

**Adapter purity — PASS:**
Only `enum.Enum` imported. No model SDK. PASS.

**Style consistency — PASS:**
Post-class `__doc__` assignment is the only available per-member docstring pattern that actually works for Python `Enum` (inline string literals after enum values are NOT assigned to `__doc__`). Verified empirically. Existing members have no `__doc__` set — the new members being the first to have docs is an intentional spec requirement, not inconsistency. Alignment of the `=` signs in the `__doc__` assignments uses trailing spaces to line up values — this matches the surrounding enum's leading-space alignment style. PASS.

**Findings:** none.

**Scope check:** Diff is exactly 8 lines added to `companion_harness/reason_codes.py`. No other files modified. PASS.

**Invariant checks:**
- Every `SpeakDecision` carries a `primary_reason_code` from `ReasonCode` (CLAUDE.md invariant): the three new members are now available for tasks 12, 13, and 15. PASS.
- "Why did you say that?" invariant #7: each new member has a `__doc__` string documenting the policy concept. PASS.

**Watch-item checks:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. PASS.
- Watch-items 8, 9, 12, 17, 18: not touched. Still active.

**Cross-PR watch-items created:** none.

**Cross-PR watch-items — current active list (unchanged):**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
17. (Active) Concrete `StreamingDuplexModel` impl must use coroutine-returns-generator pattern; construction site must enforce the contract.
18. (Active) DeicticDetector Task 6: capture `_emit()` return for causal chaining; test must use asyncio harness and assert event emission.

---

## PR #45 — feat(audio): AudioOutputController.play() async-generator API (live-loop Task 4b)  (reviewed 2026-05-14)
**ROADMAP task:** live-loop Task 4b
**Verdict:** APPROVE

**Canonical-venv pytest result:** `49 passed, 2 warnings` — +1 over baseline (48). Exactly the one new test added. PASS.

**Spec fidelity — PASS:**
Task 4b spec: "Adapt `AudioOutputController.play()` to accept an async generator / queue of `bytes` so each chunk can be forwarded to the playback sink as it arrives without waiting for the full utterance to buffer." The signature change is `list[bytes]` → `AsyncIterable[bytes]`; `for chunk in chunks:` → `async for chunk in chunks:`. All six event semantics (`assistant_audio_buffer_flushed`, `assistant_audio_stop_completed`, etc.) are preserved; the underlying event logic is not touched. PASS.

**Success criterion — PASS:**
"Existing `AudioOutputController` contract tests still pass" — confirmed: all 5 prior tests in `test_audio_output_controller.py` pass; 1 test in `test_barge_in.py` and 1 in `test_decision_provenance.py` pass after call-site migration. "New contract test drives `play()` with an async generator and confirms first-chunk playback begins before the generator is exhausted" — `test_play_async_gen_no_full_collection` uses a 20ms inter-yield `slow_gen()` generator. `sink_times[0] < yield_times[-1]` asserts the first chunk is delivered before the generator finishes. Non-vacuous: if `play()` collected the full list first, `sink_times[0]` would occur AFTER `yield_times[-1]` and the assertion would fail. PASS.

**Caller migration — PASS:**
All three call sites that previously passed `list[bytes]` are updated to pass `_agen(chunks)` — an async generator that yields from the list without buffering. Each file defines its own `_agen` locally (consistent with the project pattern of local helpers per file). No call sites in `companion_harness/` (none existed — controllers are driven by tests only at this point). PASS.

**Adapter purity — PASS:**
`AsyncIterable` imported from `collections.abc` (stdlib). No SDK added. PASS.

**Scope check:** Diff touches exactly `companion_harness/audio_output_controller.py` (2-line signature + docstring change), `tests/test_audio_output_controller.py` (1 new import, 1 new helper, 2 call-site updates, 1 new 50-line test), `tests/test_barge_in.py` (1 new import, 1 new helper, 1 call-site update), `tests/test_decision_provenance.py` (1 new import, 1 new helper, 1 call-site update). No adapter logic changed, no schema changes. PASS.

**Findings:**

- [nit / style] `tests/test_audio_output_controller.py:211,215` — `asyncio.get_event_loop().time()` inside `async def` functions. In Python 3.12 the correct idiom inside a running async context is `asyncio.get_running_loop().time()` (the running loop is guaranteed to exist; `get_event_loop()` is deprecated for this usage). Behavior is identical on all current CPython versions because inside an async function the running loop IS the event loop, but `get_running_loop()` is the modern and non-deprecated form. No behavior risk — functional test passes. Severity: nit.

**Invariant checks:**
- Invariant #10 (EventLogger non-blocking): `play()` `async for` loop still calls `self._sink(chunk)` (an awaited async call) and `self._emit()` (synchronous, non-blocking). No logger await added to the hot path. PASS.
- Invariant #1 (no unlogged behavior): event semantics unchanged — same six events emitted on same conditions. PASS.

**Watch-item checks:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed. `retention_policy_id="default"` on all events (unchanged from PR #4). PASS.
- Watch-items 8, 9, 12, 17, 18: not touched. Still active.

**Cross-PR watch-items created:** none.

**Cross-PR watch-items — current active list (unchanged):**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
17. (Active) Concrete `StreamingDuplexModel` impl must use coroutine-returns-generator pattern; construction site must enforce the contract.
18. (Active) DeicticDetector Task 6: capture `_emit()` return for causal chaining; test must use asyncio harness.

VERDICT: nits-only

---

## PR #44 — feat(ingest): add ingest_video_frame with per-modality causal pointers (v0.1c Task 1)  (reviewed 2026-05-14)
**ROADMAP task:** v0.1c Task 1
**Verdict:** APPROVE

**Canonical-venv pytest result:** `49 passed, 2 warnings` — +1 over baseline (48). Exactly one new test. PASS.

**Spec fidelity — PASS:**
Task 1 spec: "Add `ingest_video_frame(session, frame_bytes, meta)` that writes the frame blob and emits a `raw_video_frame` event with `payload_kind='raw_video'`. Maintain separate `_prev_audio_chunk_id` and `_prev_video_chunk_id` per session; each modality's `caused_by[]` chain is independent." Verified in the diff: `_prev_chunk_id` → `_prev_audio_chunk_id` (surgical rename, 2 call sites); `_prev_video_chunk_id` added; `ingest_video_frame()` with `event_type="raw_video_frame"`, `payload_kind="raw_video"`, `source=f"{client_id}.video"`. All three spec verification requirements — (a) no orphans across modalities, (b) video `caused_by` never points to audio chunk id, (c) audio `caused_by` never points to video frame id — are directly asserted in the test. PASS.

**Causal structure — PASS:**
`ingest_video_frame()` causal logic: first video frame traces to `session.session_open_id` (not to any audio chunk id — the audio chain is a sibling, not a parent); subsequent frames trace to the prior video frame's id. This is the correct parallel-chain design. The test explicitly asserts `video_events[0].caused_by == [session_open.event_id]` and `cur.caused_by == [prev.event_id]` within the video chain. Verified by CausalGraph(report.orphan_count == 0). PASS.

**Surgical scope — PASS:**
The audio path is unchanged except for the rename `_prev_chunk_id` → `_prev_audio_chunk_id`. No audio logic is modified. The two call sites in `ingest_chunk()` that read/set the old name are updated. No opportunistic refactor. PASS.

**Test quality — PASS:**
`test_interleaved_audio_video_causal_chains` exercises the three spec assertions plus: event_type and payload_kind on video events, payload_hash vs stored blob, and seq_no monotonicity. N=4 interleaved pairs. The cross-modal assertions check every `caused_by` ref in every video event against the full set of audio event ids, and vice versa — not a spot check. Non-vacuous: if `_prev_video_chunk_id` were never assigned and the video chain used `_prev_audio_chunk_id` instead, assertion (b) would fail on the second video frame (its `caused_by` would contain an audio chunk id). PASS.

**Adapter purity — PASS:**
`input_ingest.py` imports are stdlib (`asyncio`, `hashlib`, `json`, `time`, `uuid`, `dataclasses`, `datetime`, `pathlib`) + `companion_harness.event_logger` + `companion_harness.schemas`. No SDK. PASS.

**Invariant checks:**
- Invariant #1 (no unlogged behavior): `ingest_video_frame()` calls `self._logger.log(event)` synchronously (non-blocking `put_nowait`). PASS.
- Invariant #10 (EventLogger non-blocking): same pattern as `ingest_chunk()` — synchronous non-blocking call. PASS.
- `caused_by[]` non-empty: every video frame has `caused_by` = either `[session.session_open_id]` or `[session._prev_video_chunk_id]` — never empty. PASS.

**Findings:** none.

**Watch-item checks:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): `ingest_video_frame()` uses `retention_policy_id=_RETENTION` (`"raw_media_default_300s"` — non-empty). PASS.
- Watch-items 8, 9, 12, 17, 18: not touched. Still active.

**Cross-PR watch-items created:** none.

**Cross-PR watch-items — current active list (unchanged):**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
17. (Active) Concrete `StreamingDuplexModel` impl must use coroutine-returns-generator pattern; construction site must enforce the contract.
18. (Active) DeicticDetector Task 6: capture `_emit()` return for causal chaining; test must use asyncio harness.

VERDICT: clean

---

## PR #42 — feat: stub VisionSidecar with ring buffer + privacy guard (v0.1c Task 3)  (reviewed 2026-05-14)
**ROADMAP task:** v0.1c Task 3
**Verdict:** APPROVE

**Canonical-venv pytest result:** `61 passed, 2 warnings` — +13 over baseline (48). Exactly the 13 new tests in `test_vision_sidecar_stub.py`. PASS.

**Spec fidelity — PASS:**
Task 3 spec requires: (1) ring buffer bounded to 60s window keyed on `timestamp_mono_ms`, not `time.monotonic()` — verified: eviction uses `ref.timestamp_mono_ms` throughout `_evict()`; no `time.monotonic()` call anywhere in the class; (2) scene-change scorer injectable via Protocol — `SceneScorer` is `@runtime_checkable` Protocol; test injects `_FakeSceneScorer`; `isinstance` check passes; (3) deictic gate: `resolve(query, deictic_reference=False)` returns not-resolvable immediately; (4) `no_camera_memory` guard disables buffer entirely — `ingest_frame()` returns 0.0 and stores nothing; `resolve()` with gate open also returns not-resolvable. All four spec requirements verified. PASS.

**Ring-buffer eviction correctness — PASS:**
`_evict(current_ts_ms)` pops from the left while `self._buffer[0].timestamp_mono_ms <= cutoff` where `cutoff = current_ts_ms - self._window_ms`. The `<=` means a frame exactly `window_ms` old is evicted (the window is an exclusive-left, inclusive-right interval). The test `test_ring_buffer_evicts_old_frames_by_event_timestamp` uses `ref_a` at t=0, `ref_c` at t=61_000, `window_ms=60_000` → cutoff=1000; 0 <= 1000 → evicted. Correct. The boundary semantics (a frame exactly at the edge is evicted, not retained) are deterministic and consistent. PASS.

**Privacy guard non-vacuousness — PASS:**
`test_no_camera_memory_buffer_stays_empty`: ingests a frame, asserts buffer_size() == 0. If `ingest_frame()` failed to check `privacy_mode`, buffer_size would be 1 and this test would fail. PASS.
`test_no_camera_memory_resolve_returns_not_resolvable`: calls `resolve()` with `deictic_reference=True` and no frames ingested. This is structurally the same as the empty-buffer case — the two cases are not fully disentangled (the existing `test_resolve_with_empty_buffer_returns_not_resolvable` also covers empty-buffer + `deictic_reference=True`). A stronger test would ingest a frame under `no_camera_memory` and then attempt `resolve()` — verifying both that the frame was not stored AND that the guard blocks resolution independently. Existing tests together cover the behavior correctly (both branches of the guard in `resolve()` are hit) but the `no_camera_memory` resolve test provides only marginal additional coverage. This is a concern about test expressiveness, not correctness.

**Adapter purity — PASS:**
Imports: `collections.deque`, `dataclasses.dataclass`, `typing.Protocol`, `typing.runtime_checkable` — all stdlib. No model SDK. PASS.

**Protocols `@runtime_checkable` — PASS:**
Both `SceneScorer` and `GroundingModel` carry `@runtime_checkable`. `isinstance(_FakeSceneScorer([0.5]), SceneScorer)` and `isinstance(_FakeGroundingModel("mug", 0.9), GroundingModel)` are tested explicitly. PASS.

**Scope check:**
Two new files: `companion_harness/vision_sidecar.py` (167 lines) and `tests/test_vision_sidecar_stub.py` (217 lines). No existing files modified. No spec (`docs/architecture-v0.1.md`) edited. PASS.

**CLAUDE.md rule 4 (stub PR):**
Task 3 is explicitly a stub task. The class establishes the skeleton, ring-buffer logic, privacy guard, and protocol interfaces. Full grounding wiring (event logging, `caused_by` chain) is Task 7. The stub correctly omits `EventLogger` integration — no invariant #1 violation because Task 7 is where the grounding events are wired. PASS.

**`_last_frame` / eviction ordering — minor observation:**
`_last_frame` is updated for every `ingest_frame()` call regardless of whether the frame was evicted from the buffer. This means the scene-change scorer always computes against the most recent frame byte blob. This is correct for the scorer's purpose (scoring the change from the prior frame to the current one) and does not require the prior frame to still be in the ring buffer. No finding.

**Findings:**

- [concern / test-expressiveness] `tests/test_vision_sidecar_stub.py:199–206` — `test_no_camera_memory_resolve_returns_not_resolvable` calls `resolve()` without having ingested any frame under `no_camera_memory`. The test covers the same code path as `test_resolve_with_empty_buffer_returns_not_resolvable` (empty buffer → not-resolvable) plus the `privacy_mode == "no_camera_memory"` short-circuit in `resolve()`. A more expressive test would ingest a frame first, then call resolve, to demonstrate the guard independently from the empty-buffer case. The combined coverage from the existing two `no_camera_memory` tests (buffer stays empty + resolve returns not-resolvable) is correct but marginally weaker than a combined ingest-then-resolve test. Not a blocker — the existing tests do cover the spec requirement as stated ("ring buffer is empty and non-resolvable under `no_camera_memory` mode").

**Watch-item checks:**
- Watch-item 3 (SensitiveField.retention_policy_id non-empty): No SensitiveField constructed in the stub. N/A. PASS.
- Watch-items 8, 9, 12, 17, 18: not touched. Still active.

**Cross-PR watch-items created:**
19. (v0.1c Task 7 — VisionSidecar wiring) When Task 7 implements `ingest_frame()` with event logging and a closed `caused_by` chain into `raw_video_frame` events: (a) verify every grounding event's `caused_by` chain traces to the `raw_video_frame` event whose `event_id` is stored in the `FrameRef.event_id` field; (b) verify no grounding event has an empty `caused_by`; (c) confirm `EventLogger` is started before any `ingest_frame()` call and stopped after, matching the pattern in `test_video_ingest.py`.

**Cross-PR watch-items — current active list:**
3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
17. (Active) Concrete `StreamingDuplexModel` impl must use coroutine-returns-generator pattern; construction site must enforce the contract.
18. (Active) DeicticDetector Task 6: capture `_emit()` return for causal chaining; test must use asyncio harness.
19. (New) VisionSidecar Task 7: every grounding event `caused_by` must trace to the triggering `raw_video_frame` event; EventLogger must be started/stopped correctly in tests.

VERDICT: nits-only

---

## PR #41 — stub DeicticDetector: Protocol + skeleton (v0.1c Task 5) — round 2  (reviewed 2026-05-14)
**ROADMAP task:** v0.1c Task 5
**Verdict:** APPROVE — PR #41 CONVERGED

### Canonical-venv pytest result

`/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_deictic_detector_stub.py -v`
→ **2 passed** in 0.01s.

Full suite (PR branch): **50 passed, 2 warnings** in 21.75s. No regressions.

---

### Round-1 BLOCKER 1 — RESOLVED

**Scope creep (`classify()` shipping Task 6 implementation, `_next_seq()` / `_emit()` present).**

`classify()` now contains exactly `raise NotImplementedError` and a docstring that specifies what Task 6 will implement. `_next_seq()` and `_emit()` are absent from the file. Confirmed by direct read of `companion_harness/deictic_detector.py`. The class body is now: `SOURCE`, `SCHEMA_VERSION`, `__init__`, and `classify()` stub only. PASS.

### Round-1 BLOCKER 2 — RESOLVED

**Vacuous test (`test_classify_returns_model_result` + `_make_logger` helper, logger never started).**

`test_classify_returns_model_result` and `_make_logger` are absent from `tests/test_deictic_detector_stub.py`. Exactly 2 tests remain:
1. `test_fake_satisfies_protocol` — `isinstance(_FakeDeicticModel([(True, 0.9)]), DeicticModel)` asserted True. Non-vacuous: if `DeicticModel` Protocol method signature changed or `@runtime_checkable` were removed, this would fail. PASS.
2. `test_deictic_detector_construction` — constructs `DeicticDetector` with fake model, `session_id`, and a live `EventLogger` instance; asserts `isinstance(detector, DeicticDetector)`. Non-vacuous: if the constructor signature were broken, this would raise `TypeError`. PASS.

Both tests are the tests the round-1 review required. No stale imports, no vacuous assertions, no logger-never-started hazard (no event-logging assertion attempted). PASS.

---

### Holistic pass — Task 5 success criterion

"imports cleanly; isinstance Protocol check passes with a fake `DeicticModel`." Both verified by test. No invented scope, no forward-looking Task 6 scaffolding. PASS.

### Adapter purity — PASS

`companion_harness/deictic_detector.py` imports only: `__future__.annotations`, `typing.Protocol`, `typing.runtime_checkable`, `companion_harness.event_logger.EventLogger`. No torch, no onnxruntime, no model SDK. PASS.

### ADR note and Protocol shape — PASS (unchanged from round 1)

`DeicticModel` Protocol, `DeicticDetector` class constructor, `SOURCE`/`SCHEMA_VERSION` constants, module docstring ADR note — all carry over unchanged from round 1, all correct and consistent with the established detector-adapter pattern.

### Scope check — PASS

Two new files only: `companion_harness/deictic_detector.py` and `tests/test_deictic_detector_stub.py`. No existing files modified. Clean.

### New findings

None.

---

### Watch-item 17 — checked

Watch-item 17 (concrete `StreamingDuplexModel` implementation) is not touched by this PR. Still active.

### Watch-item 18 — status confirmed

Watch-item 18 (DeicticDetector Task 6 — capture `_emit()` return for causal chaining; test must use asyncio harness) remains active. The round-2 fix correctly defers all of that to Task 6. No new finding on this PR.

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
17. (Active) Concrete `StreamingDuplexModel` impl must use coroutine-returns-generator pattern; construction site must enforce the contract.
18. (Active) DeicticDetector Task 6: capture `_emit()` return for causal chaining; test must use asyncio harness.
19. (Active) VisionSidecar Task 7: every grounding event `caused_by` must trace to the triggering `raw_video_frame` event; EventLogger must be started/stopped correctly in tests.

VERDICT: clean

---

## PR #46 — feat: MiniCPMStreamingModel — as_duplex streaming path (live-loop Task 2) — round 1  (reviewed 2026-05-14)
**ROADMAP task:** Live-Loop Task 2
**Verdict:** CHANGES-REQUESTED

### Canonical-venv pytest result

`/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/ -q` (PR branch, b200 venv, CUDA available)
→ **51 passed, 2 warnings** in 49.86s. Zero failures. PR's own claim of 51 passed confirmed.

`/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_minicpm_streaming_duplex.py -v`
→ **3 passed** in 34.80s.

---

### Findings

**[BLOCKER — brevity / correctness] `companion_harness/foreground_model_minicpm.py:133` — `self._lock = threading.Lock()` is created but never acquired anywhere.**

`threading.Lock()` is assigned to `self._lock` with a comment "duplex state is stateful; one stream at a time." But `_gen()` never calls `with self._lock:` or `self._lock.acquire()`. The lock provides zero concurrent-access protection — a second concurrent call to `infer_stream()` while the first `_gen()` is running will reach `duplex.prepare(...)` on the same `self._duplex` object without any serialization. The comment overpromises what the code delivers. This is not a "defensive shim against an impossible case" — the PR's own comment identifies concurrent use as a real risk. Either (a) delete `self._lock` and remove the comment (honest: concurrent calls are not guarded), or (b) acquire the lock inside `_gen()` (but this will block the event loop since `threading.Lock` is not async-aware — use `asyncio.Lock` instead and `async with` it inside `_gen()`). Option (a) is the minimum correct fix for a stub that does not yet wire the live loop; concurrent safety is Task 5 scope. Option (b) is the correct production fix but introduces an `asyncio.Lock` that was not in scope. Per CLAUDE.md rule 2 ("Minimum code that solves the problem. Nothing speculative"), option (a) is correct for Task 2.

**[BLOCKER — brevity] `companion_harness/foreground_model_minicpm.py:36` — `import threading` is now dead code if `self._lock` is removed (option a above). Delete the import alongside the field.**

Note: If the coder instead implements option (b) (`asyncio.Lock`), `import threading` is replaced by a `asyncio.Lock` construction; `threading` is still deleted.

**[CONCERN — correctness] `companion_harness/foreground_model_minicpm.py:168` — `caused_by=list(caused_by)` freezes the caller's `caused_by` list at the time each chunk is processed.**

This is correct behavior for the first proposal yielded in a stream. However, for all subsequent proposals within the same `_gen()` invocation the `caused_by` remains the original caller's list, pointing at the top-level stream event (e.g. the `foreground_frame` event from `ForegroundModel.process_stream()`). The spec (invariant #1, `caused_by[]`) requires every event to point at its proximate causal predecessor — successive `ThinkerProposal` candidates within a streaming window should ideally trace to the prior chunk's events, not all to the same root event. For a Task 2 stub driving a basic stream this is acceptable (Task 5 orchestration will impose proper causal threading), but the PR description claims full invariant #1 compliance via `caused_by=list(caused_by)`. It satisfies structural non-emptiness but not strict causal proximity. This is a CONCERN, not a BLOCKER for Task 2, but the PR description's claim should be accurate.

**[CONCERN — brevity] `companion_harness/foreground_model_minicpm.py:139–201` — `infer_stream` body and the `_gen()` inner function are inside the method even though `_gen()` captures nothing from `infer_stream`'s local scope that could not be passed as arguments.**

`_gen()` captures `self._duplex` via `duplex = self._duplex` and `caused_by` from the outer `infer_stream` scope. The `caused_by` argument is already available at definition time. The `frame_iter` argument could be passed directly. This closure is the correct Python idiom for the coroutine-returns-generator pattern (watch-item 17 constraint), so the nested definition is not gratuitous. Not a finding — flagging for awareness only. No action required.

**[NIT — style] `tests/test_minicpm_streaming_duplex.py:71–72` — `assert isinstance(proposals, list)` is vacuously true.**

`proposals = [p async for p in gen]` produces a `list` by construction. Asserting `isinstance(proposals, list)` cannot fail. Delete this assertion; the test already has a non-vacuous check (`assert hasattr(gen, "__aiter__")`) and the behavioral assertions in the third test are the meaningful gates.

**[NIT — style] `tests/test_minicpm_streaming_duplex.py:37–44` — `_make_pcm16_tone` builds the sample list with a Python list comprehension then packs with `struct.pack(f"<{n_samples}h", *samples)`. For large `n_samples` (e.g. `int(2.0 * 16000) = 32000`), unpacking 32000 items as `*samples` into `struct.pack` may hit CPython argument-count limits on some platforms. `numpy` is already imported in the production code; using `np.array(..., dtype=np.int16).tobytes()` in the test helper would be both shorter and safe. Not a blocker — current CPython limits are typically 2^24 args, well above 32000 — but the np approach is idiomatic given the dependency is available.**

---

### Adapter-first check — PASS

`companion_harness/foreground_model.py` is not modified by this PR (confirmed by `gh pr diff 46 --name-only`: exactly `companion_harness/foreground_model_minicpm.py` and `tests/test_minicpm_streaming_duplex.py`). No torch/SDK anywhere in `foreground_model.py`. PASS.

### Additive/surgical check — PASS

`MiniCPMDuplexModel` and its `chat()` method are unchanged. The PR appends `MiniCPMStreamingModel` as a separate class after the existing class. No modification to `_MODEL_ID`, `MiniCPMDuplexModel.__init__`, `chat()`, or `infer()`. The existing `__all__` gains `"MiniCPMStreamingModel"` and the module-level constants `_SAMPLE_RATE` / `_CHUNK_SAMPLES` are added. PASS.

### Async-generator pattern — PASS

`inspect.isasyncgenfunction(MiniCPMStreamingModel.infer_stream)` → False.
`inspect.iscoroutinefunction(MiniCPMStreamingModel.infer_stream)` → True.
`infer_stream` is correctly a coroutine that returns `_gen()`, where `_gen` is the async generator function. The caller uses `gen = await model.infer_stream(...)` then `async for p in gen:`. This matches the `StreamingDuplexModel` Protocol contract and avoids `TypeError` on `await`. PASS. Watch-item 17 constraint satisfied.

### `StreamingDuplexModel` Protocol satisfaction — PASS

`isinstance(MiniCPMStreamingModel(), StreamingDuplexModel)` evaluated True in the test (confirmed by `test_streaming_model_satisfies_protocol` passing). The `infer` method signature on `MiniCPMStreamingModel` (`infer(self, audio_frame: bytes, video_frame: bytes | None = None)`) matches the Protocol's extended signature (PR #40 added the optional `video_frame` parameter). PASS.

### `ThinkerProposal.caused_by` non-empty — PASS (structural)

`caused_by=list(caused_by)` ensures the list is non-empty for any non-empty caller `caused_by`. `ForegroundModel.process_stream()` passes `[frame_evt.event_id]` as `caused_by` — non-empty by construction. Watch-item 11 (caused_by repair at adapter layer) is also present in `ForegroundModel` as backstop.

### CUDA gate pattern — PASS

`torch = pytest.importorskip("torch", reason="torch not available — b200 venv required")` + `if not torch.cuda.is_available(): pytest.skip(...)`. Mirrors `tests/test_direct_question_latency.py` exactly. PASS.

### No pip-install — PASS

`gh pr diff 46 --name-only` returns exactly two files. `requirements-b200.txt` is not modified. No new pip install in the PR. PASS.

### Rule 4 check — PASS (option b: skip-to-green)

The PR description identifies success as "51 passed, 0 failed" with the streaming integration tests passing on b200. A prior `pytest.skip` on the streaming model is converted to 3 passing tests. CLAUDE.md rule 4(b) satisfied.

---

### Watch-item 17 — RESOLVED (partially)

Watch-item 17 required: "(a) verify `infer_stream` is a coroutine that RETURNS an async generator (not an async generator function); (b) construction site enforces `StreamingDuplexModel` contract."

(a) RESOLVED — verified empirically via `inspect.iscoroutinefunction` + `isinstance(model, StreamingDuplexModel)` test.
(b) PARTIALLY OPEN — `ForegroundModel.__init__` still accepts `model: DuplexModel` and does not enforce `StreamingDuplexModel` at construction time (the PR #40 concern). This was pre-existing before Task 2; Task 2 does not make it worse. Watch-item 17(b) remains active for the Task 5 orchestrator PR that actually wires `MiniCPMStreamingModel` into `ForegroundModel`.

### Cross-PR watch-items created

20. (Live-loop Task 5 — orchestrator) When `MiniCPMStreamingModel` is injected into `ForegroundModel` in the Task 5 orchestrator: (a) verify `self._lock = threading.Lock()` is either deleted (no concurrent-call guarantee) or replaced with `asyncio.Lock` acquired inside `_gen()` (production concurrent-call guard); (b) verify successive `ThinkerProposal` `caused_by` in a multi-chunk stream points at intermediate chunk events, not always the root stream event, if invariant #1 strict causal-proximity is required by that point.

---

### Cross-PR watch-items — current active list

3. (Ongoing) `SensitiveField.retention_policy_id` must be non-empty at all call sites.
8. (Active) VAD numeric defaults must become config reads when `implementation-config.yaml` gains numeric VAD fields.
9. (Active) `quiet_mode` must NOT be implemented as a `privacy_mode` string.
12. (Active) `ThinkerProposalGen` / `ForegroundModel` API ambiguity must be resolved when Stage 6 begins.
17. (Active — (a) RESOLVED, (b) partially open) `StreamingDuplexModel` impl coroutine-returns-generator pattern confirmed; construction-site enforcement deferred to Task 5 orchestrator.
18. (Active) DeicticDetector Task 6: capture `_emit()` return for causal chaining; test must use asyncio harness.
19. (Active) VisionSidecar Task 7: every grounding event `caused_by` must trace to the triggering `raw_video_frame` event; EventLogger must be started/stopped correctly in tests.
20. (New) Task 5 orchestrator: (a) `self._lock = threading.Lock()` in `MiniCPMStreamingModel` is dead — delete or replace with `asyncio.Lock` + `async with`; (b) successive proposal `caused_by` in multi-chunk stream should point at intermediate events if strict causal proximity is enforced.

### Required changes (numbered, actionable)

1. `companion_harness/foreground_model_minicpm.py:133` — Delete `self._lock = threading.Lock()` and its comment.
2. `companion_harness/foreground_model_minicpm.py:36` — Delete `import threading`.
3. `tests/test_minicpm_streaming_duplex.py:72` — Delete `assert isinstance(proposals, list)`.

The two BLOCKERs (1 and 2 above) are a single-commit fix: delete 2 lines from `foreground_model_minicpm.py`. The NIT (3) is optional but clean.

VERDICT: dirty

---

## PR #46 — round-2 review (2026-05-14)

**Reviewer:** spec-gatekeeper (Claude Sonnet 4.6)
**Branch:** `liveloop-task-2-minicpm-as-duplex` @ `9a22b45`
**Verdict: APPROVE**

### Fix-commit scope — CONFIRMED CLEAN

Fix commit `9a22b45` ("fix: remove unused threading import and unacquired lock from MiniCPMStreamingModel") touches exactly:
- `companion_harness/foreground_model_minicpm.py` — 3 lines deleted, 0 lines added, 1 file only.

Deleted lines verified:
1. `import threading` (was line 36 in round-1 diff) — gone.
2. `self._lock = threading.Lock()  # duplex state is stateful; one stream at a time` (was line 133) — gone.

No other files were touched. Nothing else changed.

### `threading` verification — CONFIRMED ABSENT

`grep -n "threading" companion_harness/foreground_model_minicpm.py` → exit 1 (no matches). The word `threading` does not appear anywhere in the file.

### Round-1 approved content — STILL INTACT

The full `MiniCPMStreamingModel` class, async-gen pattern, CUDA-gated tests, `MiniCPMDuplexModel.chat()` text path, `__all__` export, and `StreamingDuplexModel` Protocol satisfaction are all unchanged from round-1. No scope creep introduced in the fix commit.

### Watch-item 20 update

Watch-item 20(a) — "delete or replace `self._lock`" — is now RESOLVED. The lock was deleted. Watch-item 20(b) (successive `caused_by` in multi-chunk stream) remains active for the Task 5 orchestrator.

### Canonical-venv pytest

```
/raid/yid042/venvs/companion-harness/bin/python3 -m pytest /tmp/gk-pr46/tests/ -q
51 passed, 2 warnings in 50.00s
```

Worktree: `git worktree add /tmp/gk-pr46 origin/liveloop-task-2-minicpm-as-duplex` (detached HEAD `9a22b45`). Removed after run.

### Findings

None. Both round-1 blockers are resolved. The round-1 nit (`assert isinstance(proposals, list)` redundancy) was not required; its presence is unchanged and acceptable.

VERDICT: clean
