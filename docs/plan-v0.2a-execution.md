# Plan — v0.2a Real BackgroundReasoner (MCP-primary) execution

## Status: **DRAFT** — drafted 2026-05-16.

> v0.2a is Wave 1 of the v0.2 milestone — it replaces the
> `FakeBackgroundReasoner` smart-path adapter with an
> **MCP-primary** `BackgroundReasoner`, behind a
> `BACKGROUND_REASONER` env-var dispatcher that keeps `fake` as the
> default. The LLM-direct fallback backend named in v0.2 Anchor 2 is
> **deferred to a follow-on slice (v0.2a+1)**; v0.2a's surface is
> MCP + fake only. Default flip from `fake` → `mcp` is a v0.2-final
> task per the v0.2 pinned success criterion, not v0.2a's scope.

## Goal

After this plan ships:

- A new `MCPBackgroundReasoner` concrete class implements the existing
  `BackgroundReasoner` Protocol (in
  `companion_harness/background_reasoner.py`) by wrapping the upstream
  `mcp` Python SDK inline (no extra `MCPClient` Protocol wrapper).
- `MCPBackgroundReasoner.select_and_call(request)` selects a tool via
  MCP discovery, dispatches it, and yields the Anchor 2 event chain
  (`tool_call_requested → tool_call_dispatched →
  tool_progress_event* → tool_call_completed | tool_call_cancelled`)
  with `routing_tier="smart"` stamped on `tool_call_dispatched`
  (extends v0.1f Anchor 3 — the field already exists; today only
  `FastToolDispatcher` writes it, with `"fast"`).
- A module-level `BackgroundReasonerBudgetExhausted` exception is
  raised when wall-clock or step-count budgets are exceeded; no base
  class is introduced. Each concrete reasoner keeps its own inline
  `_budget_remaining` accounting.
- Two new ConfigStore Tier-B keys (`reasoner.budget_wall_clock_s`,
  `reasoner.budget_step_count`) flow through the existing snapshot
  path in `realtime_orchestrator.py:_snapshot_config()`.
- `manual_test_console/server.py` reads the
  `BACKGROUND_REASONER={fake,mcp}` env var at startup and constructs
  the corresponding adapter. The `mcp` SDK import stays inside
  `MCPBackgroundReasoner`; `server.py` only sees the abstract
  Protocol.
- A new `signal_producer_fallback`-class event is emitted whenever
  MCP returns a tool not in the local registry (the existing
  `signal_producer_fallback` event-type, already used for
  producer-routing telemetry in
  `realtime_orchestrator.py:528`/`703`, is reused; v0.2a adds a
  schema-registry entry to formalise it).
- All v0.1f contract tests against `FakeBackgroundReasoner` continue
  to pass unchanged; the new MCP-backed contract tests pass against a
  recorded fixture MCP server.

## Pinned success criterion

> **v0.2a succeeds when `BACKGROUND_REASONER=mcp python -m
> manual_test_console.server` brings up a session that routes one
> smart-path tool call end-to-end through MCP, emits the full Anchor 2
> event chain with `routing_tier="smart"` and closed `caused_by[]`,
> respects the Tier-B wall-clock + step-count budgets (raising
> `BackgroundReasonerBudgetExhausted` on exceed), summarizes results
> into a `MemoryItem` whose `source_event_id` traces back to a
> `tool_call_completed` event (invariant #3), and a Tier-B replay run
> over the recorded event log produces bit-identical
> `PolicyInputs.tool_progress_evidence` via
> `ToolProgressEmitter.evidence_at()`. `BACKGROUND_REASONER=fake`
> remains the default and preserves every v0.1f contract test.**

## Anchor decisions — non-transient (locked)

These decisions bake into stored events, schema registries, or
external integrations and are not cheap to reverse. They derive
directly from v0.2 Anchor 2 and v0.1f Anchors 2 + 3 + 4.

### Anchor A1 — MCP-primary only at v0.2a; LLM-direct deferred to v0.2a+1

Per Codex's review of `plan-real-background-reasoner-draft.md` (PR
#251 review notes), shipping both MCP and LLM backends in one slice
diluted the success criterion. v0.2a ships **only**
`MCPBackgroundReasoner` + the existing `FakeBackgroundReasoner`.
`LLMBackgroundReasoner` (MiniCPM-o reuse — see Closed Decision OQ-D4)
ships in a follow-on slice that reuses the same Protocol surface.

**Derives from:** v0.2 Anchor 2 (MCP-primary with LLM-direct
fallback); v0.1f OQ-1 (deferral of real MCP from v0.1f).
**Replay impact:** none — the Protocol surface
(`select_and_call`/`summarize`) is unchanged from v0.1f; v0.2a only
adds a concrete that emits the same Anchor 2 event chain.

### Anchor A2 — Event-stream-first replay (Tier-B reconstructs from `tool_progress_event` only)

Tier-B replay reconstructs `PolicyInputs.tool_progress_evidence`
from the recorded `tool_progress_event` log alone — **not** from
`tool_call_completed` payloads. This matches
`ToolProgressEmitter.evidence_at()`'s existing behaviour
(`companion_harness/tool_progress_emitter.py:74-115`), which filters
the event_log iterable on `evt_type == "tool_progress_event"` and
ignores other event types.

**Derives from:** v0.1f Anchor 4 (replay determinism contract:
`ms_since_last_filler` derived from `timestamp_mono_ms` deltas
between `tool_progress_event`s); invariants #5 + #6.
**Implication for MCP:** `MCPBackgroundReasoner` MUST emit
`tool_progress_event` rows during the MCP stream (one per upstream
progress update), not just a terminal `tool_call_completed`.

### Anchor A3 — `routing_tier="smart"` stamped on `tool_call_dispatched` by the reasoner itself

Today, `routing_tier` is stamped by `FastToolDispatcher._make_event`
(`companion_harness/fast_tool_dispatcher.py:91-96`) with the literal
`"fast"`. For the smart path, the reasoner is the dispatch authority,
so `MCPBackgroundReasoner` MUST emit `tool_call_dispatched` itself
with `routing_tier="smart"`. v0.2a does **not** extend
`FastToolDispatcher` with a parameterised `routing_tier` — the two
producers stay separate (simpler, matches Anchor 3 of v0.1f which
locks routing_tier as a single recorded value, not a parameter
threaded through a shared helper).

**Derives from:** v0.1f Anchor 3 (routing_tier recorded as a field
on `tool_call_dispatched`); single-source-of-truth discipline.
**Schema:** `v0_1f_event_schema.py:91-103` already accepts
`routing_tier ∈ {fast, smart}`; v0.2a adds no schema change to this
event type. The event-emission helper inside
`MCPBackgroundReasoner` mirrors
`FastToolDispatcher._make_event()`'s shape but lives in the reasoner
module (no shared base class — see Anchor A4).

### Anchor A4 — Module-level `BackgroundReasonerBudgetExhausted`; inline `_budget_remaining` per concrete reasoner; NO base class

The exception lives at module scope in
`companion_harness/background_reasoner.py` and is importable as
`from companion_harness.background_reasoner import
BackgroundReasonerBudgetExhausted`. Each concrete reasoner
(`MCPBackgroundReasoner`, future `LLMBackgroundReasoner`) carries
its own inline `_budget_remaining_wall_clock_s: float` and
`_budget_remaining_steps: int` counters. `FakeBackgroundReasoner`
does **not** need budget enforcement (its event emission is
synchronous and bounded by construction).

**Justification:** the v0.2 draft proposed a `BackgroundReasoner`
base class for budget enforcement; per coding-discipline rule #2
("no abstractions for single-use code"), a base class for one
production concrete is premature. When the LLM concrete lands in
v0.2a+1, the two implementations will share inline patterns —
**that** is the right moment to extract a base (rule #3: don't
refactor opportunistically).
**Derives from:** spec invariant #5 (deterministic Tier-B replay);
v0.1f OQ-3 (wall-clock budget); CLAUDE.md coding rule #2.

### Anchor A5 — `set_context()` per-call injection per v0.1f OQ-7

The orchestrator's existing smart-path drain in
`realtime_orchestrator._smart_path_task()` (lines 1106-1123) calls
`self._background_reasoner.summarize(result)` then
`self._foreground_model.set_context(memory_items)`. v0.2a does NOT
change this surface — `MCPBackgroundReasoner.summarize()` returns a
`list[MemoryItem]` exactly like `FakeBackgroundReasoner.summarize()`
does today (`background_reasoner.py:89-113`).

**Derives from:** v0.1f OQ-7 (reuse `set_context()` for smart-path
summary injection; no new Protocol method).
**Invariant #3 contract:** each `MemoryItem.source_event_id` MUST
point at a recorded event in the log (the `tool_call_completed`
event id is the natural anchor; see Closed Decision OQ-D2).

## Closed decisions (OQs D1–D7, leans + justifications)

- **OQ-D1**: MCP server URL — env var only, or CLI flag too?
  **Lean: env var `MCP_SERVER_URL` only at v0.2a.** Justification:
  matches the `BACKGROUND_REASONER` env-var dispatcher pattern; CLI
  surface stays small; v0.2-final can add a CLI flag if operator
  feedback warrants one. Absent the env var when
  `BACKGROUND_REASONER=mcp`, the server fails loudly at startup
  (not silently fall back to `fake`).

- **OQ-D2**: Budget-exhaustion behaviour — raise vs yield terminal
  event vs silent truncate?
  **Lean: raise `BackgroundReasonerBudgetExhausted` after emitting a
  terminal `tool_call_cancelled` event with `caused_by[]` closing to
  the last `tool_progress_event`.** Justification: invariant #1 (no
  unlogged behaviour) — silent truncate violates the event log;
  emitting `tool_call_completed` would be lying. The cancellation
  path is the truthful terminal class. The orchestrator's
  `_smart_path_task` will need a `try/except
  BackgroundReasonerBudgetExhausted` clause that logs a
  `reasoner_budget_exhausted` event and proceeds to
  `summarize(result_with_truncation_marker)` — the partial-results
  summary still feeds `set_context()`.

- **OQ-D3**: Per-tool retry on transient error (e.g., MCP server
  network blip)?
  **Lean: NO at v0.2a.** Justification: invariant #5 — retries
  introduce non-determinism on the policy path. A transient error
  yields `tool_call_cancelled` with a documented error reason; the
  operator decides whether to re-issue. Retries are a v0.3+
  capability when the deployment posture lands.

- **OQ-D4**: LLM-fallback model choice (for v0.2a+1 follow-on, not
  v0.2a itself)?
  **Lean: MiniCPM-o reuse via the existing `ForegroundDuplexModel`
  Protocol.** Justification: avoids loading a second large model;
  the spec's Part 9 nomination of Nemotron-3 Nano Omni
  (`docs/architecture-v0.1.md:980-981`) is a deviation we document
  here — Nemotron is not yet bundled in
  `requirements-b200.txt`, and the spec is FROZEN advisory not
  prescriptive on adapter choice (the seam is what matters).

- **OQ-D5**: Unknown MCP tool handling — error vs fallback event?
  **Lean: emit `signal_producer_fallback` event with a v0.2a-new
  schema-registry entry and proceed with `tool_call_cancelled`.**
  Justification: matches the existing producer-routing telemetry
  pattern (`realtime_orchestrator.py:528`, `703`); operators get a
  single grep to find all routing-fallback rows.

- **OQ-D6**: Should `MCPBackgroundReasoner` reuse
  `FastToolDispatcher._make_event()` helper?
  **Lean: NO — duplicate the small helper inline.** Justification:
  Anchor A3 + coding rule #3 (no opportunistic refactor). The
  helper is ~30 lines; extracting it costs a shared module + import
  edits in both producers. The shared base can ship in v0.2a+1
  when the LLM reasoner needs identical emission logic.

- **OQ-D7**: Does the new ConfigStore Tier-B keys path require a
  schema-version bump?
  **Lean: NO.** Justification: `config_schema.py` adds rows
  additively (existing keys keep their values); the snapshot path in
  `realtime_orchestrator._snapshot_config()` reads new keys with
  defaults when absent. No POLICY_VERSION bump either — these keys
  affect the smart-path reasoner only; the policy decision surface
  (`SpeakPolicy.decide()`) is unchanged.

## Dependency graph

```
  T1 (deps: mcp SDK in reqs)                ─┐
  T2 (MCPBackgroundReasoner concrete)       ─┼─► T4 (env-var dispatcher in server.py)
  T3 (budget enforcement + ConfigStore keys)─┘                    │
                                                                  ▼
                                                         T5 (contract tests)
```

- T1, T2, T3 can run in parallel (T2 will fail import-time without
  T1 on b200, but local fixture tests on T2 stub out the SDK so
  parallelism is real).
- T4 gates on T2 (server constructs the concrete).
- T5 gates on T2 + T3 + T4 all merged.

## Concurrency map

- T1, T3 touch disjoint files (`requirements-*.txt` vs
  `config_schema.py` + `realtime_orchestrator._snapshot_config()`)
  — no merge conflicts.
- T2 touches only `companion_harness/background_reasoner.py` (adds
  a class to an existing module); the existing
  `FakeBackgroundReasoner` is preserved.
- T4 touches `manual_test_console/server.py` (startup wiring);
  conflicts with any other server-edit PR in flight (none expected
  for v0.2a's 1-week window).
- T5 adds new test files under `tests/` — no conflicts.

## Cross-milestone coordination

- **v0.1f.** v0.1f Anchors 2 (event-type alphabet), 3 (routing_tier
  literal), and 4 (replay-determinism contract for
  `tool_progress_evidence`) are LOCKED dependencies. v0.2a does NOT
  amend them — it ships a second producer that obeys them.
- **v0.2 Wave 0′ (Eval Phase A.5).** Independent task graph; v0.2a's
  contract tests use the same `FixtureScenarioDriver` harness that
  Wave 0′ stabilises, but the dependency direction is "v0.2a uses
  the stable Wave 0′ test bed," not "Wave 0′ blocks v0.2a."
- **v0.2 Wave 2 (diarization, POLICY_VERSION bump).** Independent;
  v0.2a does not touch `PolicyInputs` or POLICY_VERSION.
- **v0.2a+1 (LLM-direct fallback, deferred).** Will extract a shared
  base if and only if two concretes warrant one (Anchor A4
  justification).

---

## Task 1 — Add `mcp>=1.6.0` to requirements (Wave 1)

### Files touched

- `requirements-b200.txt` — add `mcp>=1.6.0`.
- `requirements-dev.txt` — add `mcp>=1.6.0` (also needed for local
  contract tests that stub the SDK at the import boundary).

### Implementation sketch

1. Append `mcp>=1.6.0` to `requirements-b200.txt` (sorted by
   convention with the rest of the file's adapter-SDK block).
2. Append `mcp>=1.6.0` to `requirements-dev.txt`.
3. Document in the PR description the pin reason (current upstream
   minor; >=1.6.0 covers the streaming-progress API used by T2).
4. **Pre-flight verification at impl time**: confirm `mcp>=1.6.0`'s
   API surface matches what T2 imports (TBD verify at impl time);
   if upstream has a newer pin we should align on, bump the pin in
   T1's PR.

### Test plan

- No new tests in T1. The success gate is "T2's contract test
  collection (`tests/test_mcp_background_reasoner_*.py`) imports
  cleanly on both local and b200 venvs after T1 + T2 merge."

### Success criterion

```
/raid/yid042/venvs/companion-harness/bin/pip install -r requirements-b200.txt
/raid/yid042/venvs/companion-harness/bin/python -c "import mcp; print(mcp.__version__)"
```

prints a version `>= 1.6.0`. Local venv same check passes.

### OQs (with leans)

- **OQ-1.1**: Pin to `>=1.6.0` or to an exact version?
  **Lean: `>=1.6.0`** — the upstream MCP SDK is still moving;
  lockfiles are out of scope until v0.3 (per v0.2 Anchor 5).
- **OQ-1.2**: Does the `mcp` SDK pull large transitive deps that
  bloat the dev venv? **TBD verify at impl time** — if yes, mark a
  follow-up to split into an optional-extra (`pip install
  .[mcp]`).

### Cross-references

- v0.2 Anchor 2 — MCP-primary backend.
- `docs/remote-dev.md` — venv conventions.

---

## Task 2 — `MCPBackgroundReasoner` concrete (Wave 1)

### Files touched

- `companion_harness/background_reasoner.py` — add
  `MCPBackgroundReasoner` class + module-level
  `BackgroundReasonerBudgetExhausted` exception. Do NOT add a base
  class (Anchor A4). The existing `FakeBackgroundReasoner` is
  preserved unchanged.
- `companion_harness/v0_1f_event_schema.py` — add a registry entry
  for `signal_producer_fallback` (formalises the existing
  event-type already emitted from
  `realtime_orchestrator.py:528`/`703`; today it has no schema row).

### Implementation sketch (12 bullets)

1. Define a module-level exception at the top of
   `background_reasoner.py`:
   ```python
   class BackgroundReasonerBudgetExhausted(RuntimeError):
       """Raised when wall-clock or step-count budget is exceeded.
       Carries: budget_kind ('wall_clock' | 'step_count'),
       limit, observed."""
   ```
2. Add `MCPBackgroundReasoner` class implementing the existing
   `BackgroundReasoner` Protocol. Constructor signature:
   ```python
   def __init__(
       self,
       mcp_server_url: str,
       session_id: str = "test-session",
       budget_wall_clock_s: float = 30.0,
       budget_step_count:   int   = 8,
   ) -> None: ...
   ```
3. The `mcp` SDK import lives **inside** the class file, after the
   existing imports; the import is at module scope (raises
   `ImportError` if the venv lacks `mcp`, which is fine — only
   sites that instantiate `MCPBackgroundReasoner` need the dep).
4. `select_and_call(request)` is an `async def` returning
   `AsyncIterator[Event]` (matches the Protocol signature in
   `background_reasoner.py:51-53`).
5. Inside `_generate_events`, instantiate an MCP client session
   pointing at `self._mcp_server_url`; the SDK call sequence is
   (a) `list_tools()` for selection, (b) `call_tool(name, args)`
   for invocation, (c) iterate the streaming progress messages.
   **TBD verify at impl time**: the exact SDK call names — pinned
   to mcp>=1.6.0 in T1.
6. Emit the Anchor 2 event chain in order. The reasoner reuses an
   inline `_make_event()` helper (duplicated from
   `fast_tool_dispatcher.py:157-194` per Anchor A3 + OQ-D6 — do
   not factor a shared base now). Source attribution: every event
   carries `source="mcp_background_reasoner"`.
7. `tool_call_dispatched` payload carries
   `routing_tier="smart"` (Anchor A3). All five tool_* event
   types use `caused_by[]` closure per v0.1f Anchor 2 ordering.
8. Each MCP progress update yields one `tool_progress_event` row
   with `progress_stage` mapped from the locked alphabet
   (`tool_progress.ProgressStage`). MCP's free-text stage labels
   map as follows: anything with `"scan"` or `"search"` → `"scanning"`;
   anything with `"aggregate"` or `"summari"` → `"aggregating"`;
   first progress → `"started"`; terminal → `"completed"`. Unknown
   stage labels default to `"scanning"` and log a
   `signal_producer_fallback` event with
   `reason="unknown_mcp_progress_stage"` (OQ-D5).
9. Budget enforcement (inline, no base class — Anchor A4):
   - On `select_and_call` entry, capture `start_mono_ms = int(time.monotonic() * 1000)`.
   - Before each MCP step, compute
     `elapsed_s = (int(time.monotonic()*1000) - start_mono_ms) / 1000`
     and `steps_taken += 1`.
   - If `elapsed_s > self._budget_wall_clock_s` OR
     `steps_taken > self._budget_step_count`: emit a
     terminal `tool_call_cancelled` event (caused_by = last
     emitted event id), then raise
     `BackgroundReasonerBudgetExhausted` with
     `budget_kind` set accordingly (per OQ-D2).
10. Unknown MCP tool (MCP returns a tool name not in local
    registry): emit `signal_producer_fallback` event with
    `reason="unknown_mcp_tool"`, then emit
    `tool_call_cancelled`, then return normally (no exception).
    The orchestrator's `_smart_path_task` keeps draining (OQ-D5).
11. `summarize(results: ToolReasonerResult) -> list[MemoryItem]`
    mirrors `FakeBackgroundReasoner.summarize()`
    (`background_reasoner.py:89-113`) — returns one
    `MemoryItem` whose `source_event_id` is set to the
    `tool_call_completed` event id (the natural causal anchor for
    invariant #3; see OQ-D2's truncation-marker semantics for the
    budget-exhausted branch where `source_event_id` becomes the
    `tool_call_cancelled` id).
12. Add a registry entry to
    `v0_1f_event_schema.py:EVENT_TYPE_SCHEMAS` for
    `signal_producer_fallback`:
    ```python
    "signal_producer_fallback": ToolEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="signal_default_30d",
        required_fields=("primary_producer", "fallback_producer", "reason"),
        notes=(
            "Producer-routing fallback telemetry. Already emitted from "
            "realtime_orchestrator (EOU + addressing fallbacks); v0.2a "
            "extends use to MCPBackgroundReasoner (unknown_mcp_tool, "
            "unknown_mcp_progress_stage). caused_by[] closes through "
            "the upstream signal that triggered the fallback."
        ),
    ),
    ```
    **TBD verify at impl time**: confirm `signal_default_30d`
    exists in `replay_privacy_policy.yaml`; if not, this PR adds it
    (additive — no policy-version impact).

### Test plan

- `tests/test_mcp_background_reasoner_event_chain.py`
  (`test_select_and_call_emits_anchor2_chain_with_smart_tier`):
  drive a fake MCP server (in-process stub) through one tool call;
  collect the yielded `Event` stream; assert the sequence is
  `tool_call_requested → tool_call_dispatched (routing_tier="smart") →
  tool_progress_event* → tool_call_completed`, with `caused_by[]`
  closing the chain.
- `tests/test_mcp_background_reasoner_unknown_tool.py`
  (`test_unknown_mcp_tool_emits_signal_producer_fallback`): MCP
  returns a tool name `"definitely_not_registered"`; assert one
  `signal_producer_fallback` event with `reason="unknown_mcp_tool"`
  fires, and the terminal event is `tool_call_cancelled`.
- `tests/test_mcp_background_reasoner_summarize_provenance.py`
  (`test_summarize_source_event_id_traces_to_tool_call_completed`):
  after a successful `select_and_call`, build a
  `ToolReasonerResult` from the recorded events, call
  `summarize(result)`, assert the returned `MemoryItem` has
  `source_event_id == <tool_call_completed.event_id>` and
  `user_visible_summary.source_event_ids[0]` matches (invariant
  #3 contract).

### Pre-flight step

Coder runs (after T1 lands):

```
/raid/yid042/venvs/companion-harness/bin/python -c \
    "from mcp import ClientSession; import mcp; print(mcp.__version__)"
```

to confirm the SDK import works and the streaming-progress API is
present in the pinned version. If not, file an issue and bump the
T1 pin in this PR (a one-line edit, OK to bundle).

### Success criterion

```
/raid/yid042/venvs/companion-harness/bin/python -m pytest \
    tests/test_mcp_background_reasoner_event_chain.py \
    tests/test_mcp_background_reasoner_unknown_tool.py \
    tests/test_mcp_background_reasoner_summarize_provenance.py -v
```

passes. All existing `FakeBackgroundReasoner` tests
(`tests/test_background_reasoner_fake.py`,
`tests/test_background_reasoner_wiring.py`) remain green.

### OQs (with leans)

- **OQ-2.1**: Does `MCPBackgroundReasoner` need to support
  multiple concurrent `select_and_call()` invocations?
  **Lean: NO at v0.2a.** Justification: the smart-path queue in
  `realtime_orchestrator._smart_path_task()` is single-consumer
  (one `await self._smart_path_queue.get()` per loop iteration —
  see `realtime_orchestrator.py:1109-1123`). Concurrency is a
  v0.3+ capability.
- **OQ-2.2**: Should the MCP session be created per-call or
  per-reasoner?
  **Lean: per-reasoner**, lazily instantiated on first
  `select_and_call`. Reuses the SDK's session-level connection
  pooling. **TBD verify at impl time** whether the MCP SDK
  enforces a single-tool-call-per-session invariant.
- **OQ-2.3**: Where does the MCP tool registry live (local-side
  allow-list)?
  **Lean: `MCPBackgroundReasoner` accepts an `allowed_tools:
  set[str] | None` constructor arg; `None` means trust whatever
  MCP returns.** `unknown_mcp_tool` fallback fires when the set is
  non-None and the returned tool is not in it.

### Cross-references

- v0.1f Anchor 2 — event-type ordering invariant.
- v0.1f Anchor 3 — `routing_tier` field literal.
- `companion_harness/fast_tool_dispatcher.py:91-96` — the existing
  `routing_tier="fast"` stamping site (mirror for the smart path).
- `companion_harness/tool_progress.py` — locked `ProgressStage`
  alphabet.
- `docs/plan-real-background-reasoner-draft.md` — original v0.2
  draft for this work (superseded by this plan).

---

## Task 3 — Budget enforcement + ConfigStore Tier-B knobs (Wave 1)

### Files touched

- `companion_harness/background_reasoner.py` — already touched by
  T2 for the exception + inline counters. T3 confirms the budget
  contract via tests + wires the ConfigStore reads.
- `manual_test_console/config_schema.py` — add two
  `TierBSchemaEntry` rows:
  `reasoner.budget_wall_clock_s` (float, default 30.0,
  `code_location="background_reasoner.py:<MCP class init>"`) and
  `reasoner.budget_step_count` (int, default 8).
- `companion_harness/realtime_orchestrator.py` — extend
  `_snapshot_config()` (around line 1138-1149) to read the two new
  keys when present and apply them to
  `self._background_reasoner` via a setter or direct attribute
  assignment. No-op when `self._background_reasoner` is None
  (preserves the v0.1f fast-path-only deployment).

### Implementation sketch (10 bullets)

1. In `config_schema.py`, append two new entries to the schema
   dict. Mirror the shape of the existing
   `"policy.backchannel_threshold"` entry (config_schema.py:49-58).
2. `reasoner.budget_wall_clock_s` entry: `key`,
   `code_location="companion_harness/background_reasoner.py"`,
   `default=30.0`, `validator=lambda v: 1.0 <= v <= 300.0`,
   `description="Wall-clock budget (seconds) per
   BackgroundReasoner.select_and_call() invocation; exceed raises
   BackgroundReasonerBudgetExhausted."`
3. `reasoner.budget_step_count` entry: same shape, `default=8`,
   `validator=lambda v: 1 <= v <= 64`,
   `description="Maximum MCP steps per
   BackgroundReasoner.select_and_call() invocation; exceed raises
   BackgroundReasonerBudgetExhausted."`
4. In `realtime_orchestrator._snapshot_config()`, after the
   existing detector + policy reads, add a block:
   ```python
   if self._background_reasoner is not None:
       wcs = cfg.get("reasoner.budget_wall_clock_s", default=None)
       sc  = cfg.get("reasoner.budget_step_count",   default=None)
       if wcs is not None and hasattr(self._background_reasoner, "_budget_wall_clock_s"):
           self._background_reasoner._budget_wall_clock_s = float(wcs)
       if sc is not None and hasattr(self._background_reasoner, "_budget_step_count"):
           self._background_reasoner._budget_step_count = int(sc)
   ```
   **TBD verify at impl time**: confirm the ConfigStore `get()`
   signature in `manual_test_console/config_store.py` (the snippet
   above guesses `default=None`; adjust to the real signature).
5. Document that `FakeBackgroundReasoner` does NOT honour the
   budget keys (no-op for it); only `MCPBackgroundReasoner`
   reads them.
6. The budget application is a hot-swap at the EOU boundary
   (existing `_snapshot_config()` cadence). New value takes effect
   on the next `select_and_call()` invocation.
7. In `_smart_path_task` (lines 1106-1123), wrap the
   `async for evt in event_iter:` loop in a `try` /
   `except BackgroundReasonerBudgetExhausted as exc:` clause. On
   exception:
   - log a `reasoner_budget_exhausted` event with
     `payload_inline={"budget_kind": exc.budget_kind,
     "limit": exc.limit, "observed": exc.observed}`;
   - still call `summarize(result)` with the
     partial-results marker so `set_context()` proceeds with
     truncated context (OQ-D2).
8. Add a `reasoner_budget_exhausted` entry to
   `v0_1f_event_schema.py:EVENT_TYPE_SCHEMAS`:
   ```python
   "reasoner_budget_exhausted": ToolEventSchema(
       payload_kind="signal",
       subject_class="self",
       sensitivity="safe",
       retention_policy_id="tool_call_audit_30d",
       required_fields=("budget_kind", "limit", "observed"),
       notes=(
           "BackgroundReasoner exceeded its wall-clock or step-count "
           "budget. caused_by[] cites the last emitted "
           "tool_progress_event from the in-flight call."
       ),
   ),
   ```
9. **No POLICY_VERSION bump.** These keys affect smart-path
   reasoner behaviour only; `SpeakPolicy.decide()` does not read
   them. (OQ-D7 closed.)
10. Add a `reasoner_budget_exhaustion_rate` gate metric definition
    to `scripts/v0_2a_replay_report.py` (deferred to v0.2-final's
    Task 20 if we don't ship that script in v0.2a; v0.2a's gate is
    measured by the contract test, not by a script — see Numeric
    gates below).

### Test plan

- `tests/test_mcp_background_reasoner_budget_wall_clock.py`
  (`test_wall_clock_budget_exhausted_raises`): construct an
  `MCPBackgroundReasoner` with `budget_wall_clock_s=0.05`
  pointing at a fake MCP server that sleeps 0.5s; assert
  `BackgroundReasonerBudgetExhausted` raised with
  `budget_kind == "wall_clock"`; assert the terminal recorded event
  is `tool_call_cancelled`.
- `tests/test_mcp_background_reasoner_budget_step_count.py`
  (`test_step_count_budget_exhausted_raises`): construct with
  `budget_step_count=2`, fake MCP server emits 5 progress steps;
  assert exception raised with `budget_kind == "step_count"` after
  step 3 emits and the 4th step would be the boundary.
- `tests/test_smart_path_handles_budget_exhausted.py`
  (`test_orchestrator_smart_path_recovers_from_budget_exhausted`):
  inject a reasoner that raises on the third
  `tool_progress_event`; assert
  `_smart_path_task` logs `reasoner_budget_exhausted`, calls
  `summarize()` on the truncated result, and calls
  `set_context()` (no crash, no orphan event).
- `tests/test_config_store_applies_reasoner_budgets.py`
  (`test_snapshot_config_applies_reasoner_budget_keys`): set both
  Tier-B keys via `config_store.patch(...)`; drive one EOU
  (triggering `_snapshot_config()`); assert the reasoner's
  `_budget_wall_clock_s` and `_budget_step_count` attributes
  reflect the patched values.

### Success criterion

```
/raid/yid042/venvs/companion-harness/bin/python -m pytest \
    tests/test_mcp_background_reasoner_budget_wall_clock.py \
    tests/test_mcp_background_reasoner_budget_step_count.py \
    tests/test_smart_path_handles_budget_exhausted.py \
    tests/test_config_store_applies_reasoner_budgets.py -v
```

passes.

### OQs (with leans)

- **OQ-3.1**: Budget defaults — 30s / 8 steps, or other?
  **Lean: 30s / 8 steps.** Justification: 30s matches v0.1f OQ-3's
  wall-clock budget; 8 steps is a small-MCP-server estimate (3-5
  for a search-and-aggregate flow + headroom).
- **OQ-3.2**: Should budget exhaustion count as a "soft failure"
  (informational metric only) or block the gate?
  **Lean: gate at < 0.05 exhaustion rate.** Matches the v0.2
  pinned success criterion clause #1 (default flip from `fake` to
  `mcp` gates on
  `background_reasoner_budget_exhaustion_rate < 0.05` for 24h of
  production use).

### Cross-references

- v0.1f OQ-3 (wall-clock budget).
- v0.2 numeric gate `background_reasoner_budget_exhaustion_rate`
  (roadmap-v0.2-draft.md line 165).
- `docs/design-config-and-dashboard.md` — Tier-B key contract.
- `manual_test_console/config_schema.py:48-72` — existing Tier-B
  entry shape.

---

## Task 4 — `BACKGROUND_REASONER` env dispatcher in `server.py` (Wave 2)

### Files touched

- `manual_test_console/server.py` — at the foreground-pipeline
  construction site, read `BACKGROUND_REASONER` env var and
  construct either `FakeBackgroundReasoner` (default) or
  `MCPBackgroundReasoner` (requires `MCP_SERVER_URL` env var).
- `manual_test_console/live_pipeline.py` — verify
  `build_live_pipeline()` accepts a `background_reasoner` param
  that flows through to the orchestrator constructor. **TBD verify
  at impl time**: today the orchestrator constructor accepts
  `background_reasoner` (see
  `realtime_orchestrator.py:237`); confirm `build_live_pipeline`
  forwards it.

### Implementation sketch (8 bullets)

1. In `server.py`, near the existing flag parsing region, add a
   helper:
   ```python
   def _construct_background_reasoner() -> "BackgroundReasoner | None":
       choice = os.environ.get("BACKGROUND_REASONER", "fake").lower()
       if choice == "fake":
           from companion_harness.background_reasoner import FakeBackgroundReasoner
           return FakeBackgroundReasoner()
       if choice == "mcp":
           url = os.environ.get("MCP_SERVER_URL")
           if not url:
               raise RuntimeError(
                   "BACKGROUND_REASONER=mcp requires MCP_SERVER_URL env var"
               )
           from companion_harness.background_reasoner import MCPBackgroundReasoner
           return MCPBackgroundReasoner(mcp_server_url=url)
       raise RuntimeError(f"Unknown BACKGROUND_REASONER={choice!r}")
   ```
2. The `mcp` SDK import (Anchor A1) stays inside
   `MCPBackgroundReasoner`, NOT in `server.py`. `server.py` sees
   only the Protocol — adapter-first invariant preserved.
3. Pass the returned reasoner into `build_live_pipeline(...,
   background_reasoner=reasoner)` at the existing call site.
4. Extend the server startup banner (the existing region near
   `print()` calls during startup) with a line:
   `f"BACKGROUND_REASONER={choice}"` for operator visibility.
5. Extend `/healthz` JSON with a `background_reasoner` key:
   `{"backend": "fake" | "mcp", "mcp_server_url": str | None,
   "budget_wall_clock_s": float, "budget_step_count": int}`. Null
   `mcp_server_url` when `backend == "fake"`.
6. Document the two env vars in
   `docs/manual-test-handbook.md` §2.1 (the env-var-and-flags
   region; **TBD verify at impl time** the exact section
   number).
7. No CLI flag at v0.2a (per OQ-D1); v0.2-final or v0.3 can add
   one.
8. **Defensive**: if `BACKGROUND_REASONER=mcp` is set but the
   `mcp` Python package is not installed (e.g., wrong venv), the
   import inside `MCPBackgroundReasoner` raises
   `ImportError` at first construction. The server's
   `_construct_background_reasoner` MUST let that error
   propagate (do NOT silently fall back to `fake` — fail loudly).

### Test plan

- `tests/test_server_background_reasoner_env_dispatcher.py`
  (`test_default_env_constructs_fake_reasoner`): patch `os.environ`
  to remove `BACKGROUND_REASONER`; call the dispatcher; assert
  `isinstance(result, FakeBackgroundReasoner)`.
- (same file, `test_mcp_env_without_url_raises`): set
  `BACKGROUND_REASONER=mcp` with no `MCP_SERVER_URL`; assert
  `RuntimeError` matching the dispatcher's error string.
- (same file, `test_mcp_env_constructs_mcp_reasoner`): set both env
  vars; assert `isinstance(result, MCPBackgroundReasoner)` and
  `result._mcp_server_url == "<patched url>"`.
- (same file, `test_unknown_env_raises`): set
  `BACKGROUND_REASONER=potato`; assert `RuntimeError`.

### Success criterion

```
/raid/yid042/venvs/companion-harness/bin/python -m pytest \
    tests/test_server_background_reasoner_env_dispatcher.py -v
```

passes. Manual smoke (b200): `BACKGROUND_REASONER=fake python -m
manual_test_console.server` boots; `BACKGROUND_REASONER=mcp
MCP_SERVER_URL=stdio://./fake_mcp_server.py python -m
manual_test_console.server` boots and `/healthz` reports
`background_reasoner.backend == "mcp"`.

### OQs (with leans)

- **OQ-4.1**: Should the dispatcher also accept a CLI flag
  (`--background-reasoner`) alongside the env var?
  **Lean: NO at v0.2a** (per OQ-D1). One surface — the env var.
  Add CLI later if operators ask.
- **OQ-4.2**: Should the constructor be called eagerly at server
  startup or lazily on first session?
  **Lean: eagerly at startup.** Justification: fail fast on
  missing env var / SDK import; matches the operator-friendly
  posture (no "everything looks fine until you try to use it").

### Cross-references

- v0.2 Anchor 2 — `BACKGROUND_REASONER={fake,mcp,llm}` env var.
- `manual_test_console/server.py` — current entrypoint.
- `companion_harness/realtime_orchestrator.py:237` — orchestrator
  constructor already accepts `background_reasoner`.

---

## Task 5 — Contract tests (Wave 3)

### Goal

Ship ≥8 contract tests that exercise the MCP-backed code path end
to end and lock the invariants. Some tests overlap with T2/T3's
per-task tests; this task ensures the full surface is covered and
that bit-identical Tier-B replay holds via recorded events.

### Files touched

- `tests/test_mcp_background_reasoner_event_chain.py` (from T2).
- `tests/test_mcp_background_reasoner_unknown_tool.py` (from T2).
- `tests/test_mcp_background_reasoner_summarize_provenance.py`
  (from T2).
- `tests/test_mcp_background_reasoner_budget_wall_clock.py`
  (from T3).
- `tests/test_mcp_background_reasoner_budget_step_count.py`
  (from T3).
- `tests/test_smart_path_handles_budget_exhausted.py` (from T3).
- `tests/test_config_store_applies_reasoner_budgets.py` (from T3).
- `tests/test_server_background_reasoner_env_dispatcher.py`
  (from T4).
- **NEW for T5**:
  `tests/test_mcp_reasoner_replay_determinism.py`
  (`test_evidence_at_replay_from_recorded_events_is_bit_identical`):
  record one full MCP `select_and_call` event stream into an
  in-memory event log; pass the log to
  `ToolProgressEmitter.evidence_at(tool_call_id, now_ms)`;
  assert the returned `ToolProgressEvidence` is bit-identical
  to a second call with the same args (invariant #5; ratifies
  Anchor A2).

### Implementation sketch (6 bullets)

1. The new replay-determinism test is the only "T5-only" test;
   all others ship in T2/T3/T4. T5's PR description lists all 8+
   tests as the coverage matrix for v0.2a.
2. Use the existing `FixtureScenarioDriver` (post-Wave 0′
   stabilisation) to set up the orchestrator + reasoner +
   in-process MCP stub.
3. The fake MCP server stub lives in `tests/conftest.py` or
   `tests/fixtures/fake_mcp_server.py` as a fixture function
   producing a context-manager-wrapped in-process MCP server. **TBD
   verify at impl time**: the MCP SDK's recommended in-process
   stub pattern (some SDKs ship `mcp.testing` helpers).
4. The recorded event log fixture for the determinism test is
   captured once via a `pytest` fixture (`recorded_mcp_event_log`)
   and reused across the test session.
5. Mirror the v0.1f `evidence_at()` contract test
   (`test_filler_evidence_bound`) but assert determinism
   end-to-end (record, then replay twice; assert byte-identical
   `ToolProgressEvidence` outputs).
6. Verify the new `reasoner_budget_exhausted` and
   `signal_producer_fallback` schema entries are honored by the
   schema-conformance test that walks every emitted event — **TBD
   verify at impl time**: the existing schema-walker test name
   (likely `tests/test_v0_1f_event_schema.py`); extend it if
   necessary.

### Success criterion

```
/raid/yid042/venvs/companion-harness/bin/python -m pytest \
    tests/test_mcp_background_reasoner_event_chain.py \
    tests/test_mcp_background_reasoner_unknown_tool.py \
    tests/test_mcp_background_reasoner_summarize_provenance.py \
    tests/test_mcp_background_reasoner_budget_wall_clock.py \
    tests/test_mcp_background_reasoner_budget_step_count.py \
    tests/test_smart_path_handles_budget_exhausted.py \
    tests/test_config_store_applies_reasoner_budgets.py \
    tests/test_server_background_reasoner_env_dispatcher.py \
    tests/test_mcp_reasoner_replay_determinism.py -v
```

all pass under `/raid/yid042/venvs/companion-harness/bin/python3`.

### OQs (with leans)

- **OQ-5.1**: Should the fake MCP server stub live in `tests/` or
  in a new `companion_harness/testing/` package?
  **Lean: `tests/`** at v0.2a (single-use helper); promote later if
  more test files need it.
- **OQ-5.2**: Do the contract tests need to run on b200, or
  local-only?
  **Lean: local-only.** The MCP stub is in-process; no GPU
  required. b200 smoke is reserved for the operator-driven check
  in T4's success criterion.

### Cross-references

- v0.1f Task 14 (`test_policy_replay_exact` extension) — Anchor A2
  parallel.
- v0.1f Task 4 (`evidence_at()` replay-determinism contract).
- `tests/test_background_reasoner_wiring.py` — existing fake
  reasoner wiring test (must remain green).

---

## Numeric gates table

v0.2a's gates are a subset of the v0.2 milestone gates
(`docs/roadmap-v0.2-draft.md` lines 162-185), filtered to those
this slice can land. Each gate cites the event type it counts +
which test or script aggregates it.

| Metric | Gate | Owner | Event type measured | Aggregator |
|---|---|---|---|---|
| `background_reasoner_budget_exhaustion_rate` | < 0.05 (24h prod tolerance; v0.2a measures via contract test only) | T3 owner | `reasoner_budget_exhausted` / total `tool_call_dispatched` with `routing_tier="smart"` | `test_mcp_background_reasoner_budget_wall_clock` + `..._step_count` (assert exception under exhaustion; rate-aggregation deferred to v0.2-final replay-report) |
| `background_reasoner_summary_set_context_attribution_rate` | == 1.0 | T2 owner | Each `set_context()` call has a `MemoryItem.source_event_id` pointing at a recorded `tool_call_completed` OR `tool_call_cancelled` event | `test_mcp_background_reasoner_summarize_provenance` |
| `tool_call_caused_by_closure_rate` (v0.1f carry-forward) | == 1.0 | T2 owner | Every `tool_*` event emitted by `MCPBackgroundReasoner` has non-empty `caused_by[]` closing to the upstream signal | `test_mcp_background_reasoner_event_chain` |
| `routing_tier_smart_attribution_rate` | == 1.0 | T2 owner | Every `tool_call_dispatched` emitted by `MCPBackgroundReasoner` carries `payload_inline["routing_tier"] == "smart"` | `test_mcp_background_reasoner_event_chain` (Anchor A3) |
| `mcp_fallback_rate` (informational at v0.2a) | informational; baseline | T2 owner | `signal_producer_fallback` with `reason ∈ {unknown_mcp_tool, unknown_mcp_progress_stage}` / total `select_and_call` | `test_mcp_background_reasoner_unknown_tool` (per-case gate; rate aggregation deferred) |
| `evidence_at_replay_determinism_rate` | == 1.0 | T5 owner | Two `evidence_at()` calls on the same recorded `tool_progress_event` log produce byte-identical `ToolProgressEvidence` | `test_mcp_reasoner_replay_determinism` (Anchor A2 / invariant #5) |
| `local_ci_pass_rate` | == 1.0 | each task owner | N/A (test-pass aggregator) | local pytest on the v0.2a test suite |
| `remote_smoke_pass_rate` | == 1.0 | each task owner | N/A | b200 smoke (`BACKGROUND_REASONER=mcp` startup + one tool call) |

All v0.1a–v0.1j gates carry forward unchanged (v0.2a does not
amend any).

## Risks + mitigations

1. **MCP SDK API churn.** The `mcp` Python SDK is moving; the
   pinned `>=1.6.0` may bump out from under us. **Mitigation:**
   T1 documents the pin reason; the v0.3 deployment posture
   introduces a lockfile.
2. **Fake MCP server stub fragility.** In-process MCP stubs can
   diverge from real-server behaviour, masking integration bugs.
   **Mitigation:** T4 success criterion includes a manual b200
   smoke against a real (or near-real) MCP server.
3. **Budget defaults too aggressive.** 30s / 8 steps may cause
   real-world tools to flap into `budget_exhausted` more than
   expected. **Mitigation:** ConfigStore Tier-B keys let
   operators tune at runtime without code edits.
4. **Schema-walker test surprise.** Adding two new event types
   (`signal_producer_fallback` is a *first* schema row for an
   already-emitted type; `reasoner_budget_exhausted` is brand
   new) may break a schema-conformance test that asserts the
   schema dict is exhaustive. **Mitigation:** T2 + T3 both touch
   `v0_1f_event_schema.py` so the additions land in lock-step
   with the producers.
5. **Default-stays-`fake` confusion.** Operators may believe v0.2a
   "ships MCP" and forget to set `BACKGROUND_REASONER=mcp`.
   **Mitigation:** the startup banner prints the active backend
   (T4 bullet 4); `/healthz` reports the same.
6. **LLM-fallback drift.** Deferring LLM-direct to v0.2a+1 means
   the Protocol surface must absorb both reasoners cleanly. If
   v0.2a's `MCPBackgroundReasoner` accidentally couples to MCP
   specifics in `summarize()`, v0.2a+1 will re-do work.
   **Mitigation:** Anchor A5 reuses the existing
   `summarize() -> list[MemoryItem]` shape — LLM concrete
   reuses the same return type unchanged.

## §Coordination notes

- **No POLICY_VERSION bump.** v0.2a does not change
  `SpeakPolicy.decide()`'s inputs or outputs; the smart-path
  reasoner only feeds `set_context()` on the foreground model
  (per Anchor A5 + v0.1f OQ-7). The next bump is v0.1k at
  diarization land (v0.2 Wave 2, Task 9).
- **v0.1f Anchor 4 contract preserved.** `evidence_at()` reads
  `tool_progress_event` rows only (Anchor A2). The MCP producer
  must emit one such row per upstream progress update; missing
  rows = silent replay drift.
- **Adapter-first.** `manual_test_console/server.py` MUST NOT
  import `mcp`; the SDK import lives only inside
  `companion_harness/background_reasoner.py`. Same discipline as
  every other adapter in the repo.
- **Single-PR-per-task.** T1, T2, T3, T4, T5 are five separate
  PRs. T2 and T3 both edit `background_reasoner.py` — T3 starts
  from T2's branch tip (or rebases before opening PR).

## §Cross-references

- `docs/roadmap-v0.2-draft.md` — v0.2 umbrella roadmap; v0.2a is
  Wave 1.
- `docs/roadmap-v0.1f-draft.md` — defines the
  `BackgroundReasoner` Protocol seam this slice fills with a
  real concrete.
- `docs/plan-real-background-reasoner-draft.md` — the original
  v0.2 design draft (PR #251, closed); this plan is its
  factually-grounded successor.
- `companion_harness/background_reasoner.py` — `BackgroundReasoner`
  Protocol + `FakeBackgroundReasoner` (the seam to extend).
- `companion_harness/fast_tool_dispatcher.py` — mirrored emission
  shape for the `routing_tier="fast"` path (Anchor A3 pattern
  source).
- `companion_harness/tool_progress_emitter.py` — `evidence_at()`
  replay-determinism contract (Anchor A2).
- `companion_harness/v0_1f_event_schema.py` — event-type schema
  registry (T2 + T3 both add rows).
- `docs/architecture-v0.1.md` §Stage 5 (lines 562-597) — spec
  governance for tool routing + filler budget.
- `docs/architecture-v0.1.md` §Part 9 (lines 980-981) — Nemotron-3
  Nano Omni nomination (deviation noted in OQ-D4).
- `CLAUDE.md` — coding-discipline rules #1-#4 (cited per Anchor
  A4 for the no-base-class decision).

## §Out of scope (deferred to v0.2a+1 or later)

- **LLM-direct fallback (`LLMBackgroundReasoner`).** Deferred to
  v0.2a+1 per Anchor A1. Uses the same `BackgroundReasoner`
  Protocol surface; MiniCPM-o reuse via the existing
  `ForegroundDuplexModel` (OQ-D4).
- **Default flip `BACKGROUND_REASONER=mcp`.** v0.2-final task per
  v0.2 pinned success criterion clause #1; gates on
  `background_reasoner_budget_exhaustion_rate < 0.05` over 24h of
  production use.
- **CLI flag `--background-reasoner`.** v0.2-final or v0.3, per
  OQ-D1 / OQ-4.1.
- **MCP-server allow-list configuration UI.** OQ-2.3 leaves it as
  a constructor arg; ConfigStore exposure is v0.3.
- **Per-tool retries on transient MCP error.** OQ-D3 — v0.3+.
- **Recursive reasoner-calling-reasoner.** Per v0.2 §Out of scope
  — never in v0.2 scope.
- **Custom MCP server implementation.** Per v0.2 §Out of scope.
- **`MCPClient` Protocol wrapper.** Per the original draft this
  was bundled with Task 2; this plan inlines the SDK into
  `MCPBackgroundReasoner` (per CLAUDE.md coding rule #2 — no
  abstractions for single-use code). Extract when a second
  consumer materialises.

---

**End of plan.** Sign-off pending project-lead review +
plan-critic convergence.
