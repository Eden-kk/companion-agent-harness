# Plan — Dashboard model-picker Phase 1 execution (hot-swap 2-state toggle)

## Status

**DRAFT — drafted 2026-05-16. Pre-plan-critic.** Convention follows other
`plan-*-execution.md` docs in this directory (e.g.
`plan-eval-phase-a-execution.md`, `plan-v0.1j-execution.md`).

This is the execution plan for **Phase 1** of the dashboard model-picker
design in `docs/plan-dashboard-model-picker-draft.md` (merged 2026-05-15
via PR #270). Phase 1 ships only the 2-state hot-swap toggle for the 12
hot seams. Cold seams (Phase 3) and multi-impl dropdowns (Phase 2) are
explicitly out of scope.

**Scope split:** five sub-PRs (D1–D5) with a file-conflict-aware
dispatch map so D2/D3/D4 can fan out in parallel after D1 lands. Each
sub-PR is independently reviewable, ships its own success criterion, and
respects CLAUDE.md rule 4 ("one PR, one outcome").

---

## Pinned success criterion (carried from PR #270 §Phase 1)

> The manual-test dashboard exposes a "Hot seams" panel listing the 12
> hot seams; each row is a 2-state toggle (`real` / `disabled`); toggling
> a row issues `POST /config/model-swap`, mutates the per-seam state in
> ConfigStore, and emits a `model_swap_completed` (or
> `model_swap_rejected`) event; `build_live_pipeline()` reads the
> per-seam state from ConfigStore at session-construction time;
> `model_swap_audit_completeness == 1.0` and
> `hot_swap_latency_ms_p95 < 100` hold in the new contract tests.

---

## §Foundation (cross-cutting decisions pre-resolved before D1)

These anchor calls affect more than one sub-task. Resolving them in the
foundation section keeps per-task sketches surgical (CLAUDE.md rule 3).

### F1 — Per-seam state lives in ConfigStore (not a parallel store)

ConfigStore (`manual_test_console/config_store.py`) is already the
server-global singleton for Tier-B runtime config (see
`docs/design-config-and-dashboard.md` §3). Phase 1 extends it with a
**second namespace** for per-seam enabled/disabled state — not a new
class. Rationale:

- Reuses the existing operator-action → config_change pattern at
  `manual_test_console/server.py:256–333`.
- Replay-layer integration point is identical (one store to fold over).
- The merged design's Tier-D taxonomy lives next to Tier-B inside the
  same `current_state()` snapshot.

Implementation: extend `ConfigStore` with `seam_state: dict[str, bool]`
and three new methods: `get_seam(seam: str) -> bool`,
`set_seam(seam: str, enabled: bool) -> SeamStateChange`,
`current_seam_state() -> dict[str, bool]`. The existing `_state` /
`current_state()` for Tier-B keys is untouched (surgical edit).

The 12 hot-seam names are enumerated in a new module-level constant
`HOT_SEAMS: tuple[str, ...]` in `config_schema.py` so D3 (UI) and D4
(live_pipeline) read from one source. Names mirror the PR #270 §Hot
seams table column 2:

```
("vad", "smart_turn", "backchannel", "asr", "tts",
 "scene_scorer", "grounding_model", "av_conflict_scorer",
 "urgency_scorer", "embedder", "attachment_risk_monitor",
 "fast_tool_dispatcher")
```

All 12 default to `enabled=True` to match the current `--enable-*`
behavior (operator must explicitly disable; surgical to the v0.1g
default).

### F2 — `disabled` semantics: factory returns None

Per Anchor 2 of PR #270, `disabled = factory returns None →
orchestrator's None-handling produces neutral behavior`. The neutral
behavior already exists for every hot seam:

| Seam | Neutral path (`disabled` → orchestrator behavior) |
|---|---|
| `vad` | falls back to `EnergyVADModel` (existing `live_pipeline.py:548–549`) |
| `smart_turn` | falls back to `SilenceSmartTurnModel` |
| `backchannel` | falls back to `ZeroBackchannelModel` |
| `asr` | falls back to `EmptyTranscriptASRModel` |
| `tts` | falls back to `NoopTtsAdapter` |
| `scene_scorer` | `_NullSceneScorer` (existing server.py:399) |
| `grounding_model` | `_NullGroundingModel` (existing server.py:400) |
| `av_conflict_scorer` | `_NullAudioVisualConflictScorer` (existing) |
| `urgency_scorer` | `_NullUrgencyScorer` (existing) |
| `embedder` | `_NullEmbeddingAdapter` (existing) |
| `attachment_risk_monitor` | bypass `late_subscribe` (new conditional) |
| `fast_tool_dispatcher` | skip dispatcher wiring (already conditional in v0.1g) |

The orchestrator None-handling is **already in place** for 11 of 12
seams from PR #255 (the `--enable-*` opt-in pattern). The only new
None-path is `attachment_risk_monitor` (D4 task).

### F3 — Event payload shape (OQ resolution)

Three new event types, all carrying `caused_by=[operator_action.event_id]`
per the existing pattern at `manual_test_console/server.py:256–333`:

```python
# model_swap_requested
{"seam": str,
 "from_enabled": bool,
 "to_enabled": bool,
 "requested_at_ms": int}

# model_swap_completed
{"seam": str,
 "from_enabled": bool,
 "to_enabled": bool,
 "applied_at_ms": int,
 "latency_ms": int,
 "operator_action_event_id": str}

# model_swap_rejected
{"seam": str,
 "attempted_enabled": bool,
 "reason": Literal["unknown_seam", "no_session_to_swap", "internal_error"],
 "error_class": str | None}
```

All three share `retention_policy_id="config_change_30d"` (existing
bucket, no new retention wiring). `model_restart_queued` /
`model_restart_applied` are Phase 3 — not added in Phase 1.

**Rationale on the 2-state `from_enabled`/`to_enabled` boolean** vs the
merged design's `from_state: "real"|"disabled"`: identical information,
fewer string-literals to typo. The string form can be derived at
replay-render time via `"real" if enabled else "disabled"`.

### F4 — How `disabled` reaches `build_live_pipeline()` (OQ resolution)

**Lean: ConfigStore patch (not env var, not CLI flag).** Rationale:

- ConfigStore is already the operator-tunable singleton (Tier-B
  precedent).
- Env vars require a process restart; the whole point of Phase 1 is
  *hot* swap.
- A CLI-flag-only path would force the dashboard to reach into argparse
  state at runtime, breaking the adapter-first discipline.

Implementation: `build_live_pipeline()` receives the `ConfigStore`
(already does, since v0.1f) and reads `config_store.get_seam(seam)` for
each of the 12 hot seams **at session-construction time**. If
`get_seam(...) is False`, the corresponding factory argument is forced
to `None` (which the existing None-handling converts to the neutral
fallback per F2).

**Hot-swap boundary** (resolving OQ-2 from merged design): the swap is
applied at the **next session-construction boundary**. Existing live
sessions keep their currently-wired adapters until the operator
reconnects. This is monotonically simpler than mid-frame swap, matches
the merged design's OQ-2 lean ("next frame boundary"), and avoids any
need to teardown live orchestrator state on a `POST /config/model-swap`.

**Future hot-swap-during-live-session** is explicitly Phase 2 / OQ — not
in Phase 1 scope. The Phase 1 latency gate (`<100 ms`) measures the
HTTP-roundtrip + ConfigStore mutation only; it does not include
adapter-rebuild latency (which is amortized to the next session's
construction).

### F5 — CLI flags remain (OQ resolution)

**Lean: do NOT deprecate the 6 `--enable-*` flags from PR #255.** The
flags set the *startup default* for each seam's enabled bit in
ConfigStore. The dashboard *overrides* that default at runtime.
Operators who script repro via shell can still pass `--enable-vad
--enable-tts ...`; the dashboard becomes additive, not replacement.

Implementation: `build_app()` in `manual_test_console/server.py:823`
gains a `seam_defaults: dict[str, bool] | None = None` kwarg. When
provided, ConfigStore's `seam_state` initializes from this dict instead
of all-True. Tests pass a dict; the CLI mainpath maps `args.enable_*`
booleans into the same dict.

### F6 — Foreground model is NOT a hot seam (Phase 3 boundary)

The PR #270 §Cold seams table lists 5 cold seams. None of them appear
in `HOT_SEAMS` (F1). If a future operator/test issues `POST
/config/model-swap` with `seam="foreground"` (or any other non-hot
name), the server returns HTTP 403 and emits a `model_swap_rejected`
event with `reason="unknown_seam"`. The cold-panel UI lands in Phase 3,
not Phase 1.

### F7 — Replay-layer integration is documented, not enforced (Phase 1)

The merged design's `replay_determinism_after_swap == 1.0` gate
(numeric gate row 3) is **structurally enforced in Phase 1** via the D5
contract test, which:

1. Drives two `POST /config/model-swap` calls in a fixture session.
2. Asserts the event log contains exactly two `model_swap_completed`
   events between the corresponding `operator_action` roots.
3. Asserts `caused_by[]` closure (no orphan events).

The full Tier-B-replay-spans-between-swaps verification (per-span
breakdown in `scripts/v0_1h_replay_report.py`) is **deferred to
Phase 1.5** (a follow-up PR that lands once `replay.py` has a callable
API — same deferral as eval Phase A's F4). Phase 1 ships the event
shape that makes the verification possible.

---

## §Sub-task split with file-conflict map

Five sub-PRs. File-conflict-aware: D2, D3, D4 fan out in parallel after
D1 merges, but **never touch the same file** (only D1 and D2 both touch
`server.py`; D2 is gated on D1).

| Sub-PR | Files touched | Lines added (est.) | Depends on |
|---|---|---|---|
| **D1: API + ConfigStore extension** | `manual_test_console/config_store.py` (extend), `manual_test_console/config_schema.py` (add `HOT_SEAMS`), `manual_test_console/server.py` (add `POST /config/model-swap` + `GET /config/seams` routes + handler stubs) | ~180 | none |
| **D2: Event types + audit emission** | `companion_harness/schemas.py` (add 3 event_type string constants, if a registry exists), `manual_test_console/server.py` (extend `_make_*_event` helpers, wire D1's route stubs to emit events) | ~120 | D1 |
| **D3: Frontend toggle UI** | `manual_test_console/index.html` (new `#hot-seams-panel` section + JS handlers) | ~150 | D1 |
| **D4: Live-pipeline factory integration** | `manual_test_console/live_pipeline.py` (read `config_store.get_seam(...)` at session construction; gate the 12 factory args; new `_NullAttachmentRiskMonitor` if needed) | ~80 | D1 |
| **D5: Contract tests** | `tests/test_dashboard_model_swap.py` (per-seam toggle round-trip), `tests/test_model_swap_events.py` (audit completeness + `caused_by` closure), `tests/test_dashboard_html_renders_seam_toggles.py` (HTML smoke), `tests/test_live_pipeline_reads_seam_state.py` (factory gating) | ~250 (4 test files) | D2 + D3 + D4 |

**File-conflict matrix** (no two parallel sub-PRs touch the same file):

|              | D1 | D2 | D3 | D4 | D5 |
|--------------|----|----|----|----|----|
| `config_store.py` | W | — | — | R | R |
| `config_schema.py` | W | — | R | R | R |
| `server.py` | W | W (sequential after D1) | — | — | R |
| `schemas.py` (companion_harness) | — | W | — | — | R |
| `index.html` | — | — | W | — | R |
| `live_pipeline.py` | — | — | — | W | R |
| `tests/test_dashboard_model_swap.py` | — | — | — | — | W |

(W = writes, R = reads/imports.) D2 is the only sub-PR that re-touches
`server.py` after D1 — that's why D2 is sequential after D1, not
parallel.

---

## §Tasks

### Task D1 — API surface + ConfigStore extension

**Files touched:**
- `manual_test_console/config_store.py` (extend — add seam-state
  namespace)
- `manual_test_console/config_schema.py` (add `HOT_SEAMS` constant +
  `validate_seam_patch()` helper)
- `manual_test_console/server.py` (add 2 routes + handler stubs that
  return 200 with empty event_id placeholder — actual event emission
  lands in D2)

**Implementation sketch:**
1. In `config_schema.py`, add:
   ```python
   HOT_SEAMS: tuple[str, ...] = (
       "vad", "smart_turn", "backchannel", "asr", "tts",
       "scene_scorer", "grounding_model", "av_conflict_scorer",
       "urgency_scorer", "embedder", "attachment_risk_monitor",
       "fast_tool_dispatcher",
   )

   def validate_seam_patch(seam: str, enabled: object) -> tuple[bool, str]:
       """Mirror of validate_patch() for seam toggles."""
       if seam not in HOT_SEAMS:
           return False, f"unknown seam: {seam!r}"
       if not isinstance(enabled, bool):
           return False, f"enabled must be bool, got {type(enabled).__name__}"
       return True, ""
   ```
2. In `config_store.py`, add a `SeamStateChange` dataclass (mirrors
   `ConfigChange` shape: `seam`, `previous_enabled`, `new_enabled`).
3. Extend `ConfigStore.__init__` with `seam_defaults: dict[str, bool] |
   None = None` kwarg; initialize `self._seam_state` to `{seam: True
   for seam in HOT_SEAMS}`, then overlay any provided defaults.
4. Add `ConfigStore.get_seam(seam) -> bool`,
   `ConfigStore.set_seam(seam, enabled) -> SeamStateChange`,
   `ConfigStore.current_seam_state() -> dict[str, bool]`. Same
   `KeyError`-on-unknown semantics as `get()`/`set()`.
5. In `server.py`, add two route handlers:
   - `GET /config/seams` returning `{"seams": [{"seam": str,
     "enabled": bool} for seam in HOT_SEAMS]}`.
   - `POST /config/model-swap` accepting `{seam: str, enabled: bool}`;
     validates via `validate_seam_patch`; on success, calls
     `config_store.set_seam(...)` and returns `{"accepted": true,
     "model_swap_event_id": "<placeholder>"}`. D2 replaces the
     placeholder with a real event_id; D1's handler returns a
     synthesized `"pending-d2-wiring"` string so the route is testable.
   - Wire both with `app.router.add_get` / `add_post` calls next to the
     existing `/config/patch` registration at `server.py:951–953`.
6. Register the seam-state namespace in the existing `GET /config`
   response so the dashboard can read both Tier-B and seam state in one
   round-trip (extend the JSON with a top-level `"seams":
   config_store.current_seam_state()` key).
7. Update `build_app()` signature with a new `seam_defaults: dict[str,
   bool] | None = None` kwarg (per F5); pass it through to the
   `ConfigStore(ALLOWLIST, seam_defaults=...)` construction at
   `server.py:910`. Do NOT wire CLI argparse mapping in D1 — that's a
   D4 detail (since D4 also touches the mainpath wiring).
8. Do NOT touch `live_pipeline.py` in D1 (that's D4).
9. Do NOT emit events in D1 (that's D2). The handler stub logs a TODO
   comment citing D2.

**Test plan:**
- `tests/test_config_store_seam_state.py` (new, ships in D1):
  - Default state: all 12 seams enabled.
  - `set_seam("vad", False)` flips state and returns
    `SeamStateChange(seam="vad", previous_enabled=True,
    new_enabled=False)`.
  - `set_seam("nonsense", True)` raises `KeyError`.
  - `seam_defaults={"vad": False}` constructor initializes vad to
    disabled.
- `tests/test_server_routes_seam_state.py` (new, ships in D1):
  - `GET /config/seams` returns all 12 seams with `enabled=True`.
  - `POST /config/model-swap` with valid body returns 200 and
    `accepted=True`.
  - `POST /config/model-swap` with `seam="bogus"` returns 403.
  - `POST /config/model-swap` with `enabled=1` (int, not bool) returns
    400.
- Existing `tests/test_config_store.py` must remain green (no
  regression on Tier-B state).

**Success criterion:**
- `pytest tests/test_config_store_seam_state.py
  tests/test_server_routes_seam_state.py` passes.
- `pytest tests/test_config_store.py
  tests/test_server_config_routes.py` (existing) no regressions.

**Anchors locked:**
- F1 (ConfigStore is the per-seam state holder; no parallel store).
- F4 (state mechanism is ConfigStore, not env var / CLI).
- F5 (CLI flags remain; dashboard overrides defaults).
- F6 (foreground/cold seams reject as `unknown_seam`).

**OQs pre-resolved:**
- *OQ: should `HOT_SEAMS` be a frozenset or a tuple?* — **Lean: tuple.**
  UI order matters (PR #270 §UI sketch fixes the row order); frozenset
  loses that. Membership check stays O(N=12) which is fine.

---

### Task D2 — Event types + audit emission

**Files touched:**
- `companion_harness/schemas.py` (only if a string-typed event registry
  exists; otherwise no change — event_type is a free `str` field per
  `schemas.py` line ~120)
- `manual_test_console/server.py` (extend `_make_operator_action_event`
  reuse; add `_make_model_swap_event` family; wire to D1's route
  stubs)

**Implementation sketch:**
1. Inspect `companion_harness/schemas.py` for an `event_type` enum or
   registry. As of v0.1g, `event_type` is a free `str` field; no
   registry exists. If a registry exists at D2-implementation time, add
   three constants:
   ```python
   EVENT_TYPE_MODEL_SWAP_REQUESTED = "model_swap_requested"
   EVENT_TYPE_MODEL_SWAP_COMPLETED = "model_swap_completed"
   EVENT_TYPE_MODEL_SWAP_REJECTED = "model_swap_rejected"
   ```
   Otherwise: no schema change; just use the literal strings.
2. In `server.py`, add three new event constructors next to
   `_make_config_change_event` at line 296:
   ```python
   def _make_model_swap_requested_event(*, seam, from_enabled, to_enabled,
                                         operator_action_event_id,
                                         seq_counter): ...
   def _make_model_swap_completed_event(*, seam, from_enabled, to_enabled,
                                         applied_at_ms, latency_ms,
                                         operator_action_event_id,
                                         seq_counter): ...
   def _make_model_swap_rejected_event(*, seam, attempted_enabled, reason,
                                        error_class,
                                        operator_action_event_id,
                                        seq_counter): ...
   ```
   All three follow the exact pattern of `_make_config_change_event`:
   `subject_class="self"`, `sensitivity="safe"`,
   `retention_policy_id="config_change_30d"`,
   `caused_by=[operator_action_event_id]`, `payload_kind="signal"`.
3. Wire the `POST /config/model-swap` handler (D1 stub) to:
   - Emit `operator_action` event FIRST (reuse
     `_make_operator_action_event` with `endpoint="/config/model-swap"`).
   - Time the ConfigStore mutation with `time.monotonic_ns()` for the
     `latency_ms` measurement.
   - Emit `model_swap_requested` immediately before the mutation.
   - On `validate_seam_patch` success + mutation success: emit
     `model_swap_completed` with measured `latency_ms`.
   - On `validate_seam_patch` failure: emit `model_swap_rejected` with
     `reason="unknown_seam"` (HTTP 403) or — there is no other current
     failure mode in Phase 1 since the actual adapter rebuild is
     deferred to next session-construction per F4. `reason="internal_error"`
     is reserved for future hot-swap-during-session work and is wired
     but not exercised in Phase 1.
   - Return JSON `{accepted, model_swap_event_id:
     completed_or_rejected_event_id, requested_at_ms, applied_at_ms,
     latency_ms}`.
4. Replace D1's `"pending-d2-wiring"` placeholder with the real
   `cc_event.event_id`.
5. Do NOT touch the GET endpoints (already correct from D1).
6. Do NOT touch ConfigStore (D1 territory).

**Test plan:**
- `tests/test_model_swap_event_emission.py` (new, ships in D2):
  - One `POST /config/model-swap` produces exactly 3 events in order:
    `operator_action` → `model_swap_requested` → `model_swap_completed`.
  - All three share the operator_action root via `caused_by`.
  - `latency_ms` field present and >= 0.
  - One `POST /config/model-swap` with invalid seam produces exactly 2
    events: `operator_action` → `model_swap_rejected` (with
    `reason="unknown_seam"`).
- Existing audit tests (`tests/test_config_audit_emission.py`) must
  remain green.

**Success criterion:**
- `pytest tests/test_model_swap_event_emission.py` passes.
- `pytest tests/test_config_audit_emission.py` (existing) no
  regressions.
- `grep -n "pending-d2-wiring" manual_test_console/server.py` returns
  empty.

**Anchors locked:**
- F3 (event payload shape — 3 event types, all reusing
  `config_change_30d` bucket).

**OQs pre-resolved:**
- *OQ: should `model_swap_requested` and `_completed` be one event with
  status?* — **Lean: two events.** Matches PR #270 Anchor 3 explicitly.
  Replay must distinguish "operator wanted X" from "system applied X" —
  a future async/queued swap (Phase 2+) breaks that conflation.
- *OQ: do we emit `model_swap_completed` when `from_enabled ==
  to_enabled` (no-op swap)?* — **Lean: yes.** Audit completeness gate
  requires "every dashboard swap produces an event"; a "you clicked but
  nothing changed" event is still operator-visible and worth logging.
  The payload's `from_enabled == to_enabled` makes the no-op
  recognizable at replay time.

---

### Task D3 — Frontend toggle UI

**Files touched:**
- `manual_test_console/index.html` (new tuning-drawer section above the
  existing 3 sections; new JS handlers)

**Implementation sketch:**
1. Add a new `<div class="tuning-section" id="hot-seams-section">`
   above the existing 3 sections inside `#tuning-body`. Section header
   reads "Hot seams (12)" with a section-reset button that flips all
   12 back to enabled.
2. Render 12 `<div class="seam-row">` rows, one per `HOT_SEAMS` entry.
   Each row contains:
   - Label: human name (`vad` → "VAD", `smart_turn` → "Smart-turn", …)
     via a 12-entry lookup table inline in JS.
   - 2-state toggle: a single `<button class="seam-toggle">` rendering
     either `[● on  ○ off]` or `[○ on  ● off]`.
3. JS handler on page load: `fetch("/config/seams")` once, populate
   each toggle from the response.
4. JS handler on toggle click:
   - Optimistically flip the visual state.
   - `fetch("/config/model-swap", {method: "POST", body: JSON.stringify(
     {seam, enabled: !current})})`.
   - On `accepted=true` response: keep optimistic state; green-flash
     the row for ~250ms (reuse the existing `.green-flash` animation
     class at index.html:63).
   - On 4xx response: revert the visual state; red-flash the row
     (reuse `.red-flash`).
5. JS handler: listen for `model_swap_completed` events on the existing
   display WebSocket; if the event's `seam` doesn't match the
   client-side optimistic state, reconcile (this handles the
   multi-tab case).
6. CSS reuses the existing `.slider-row` / `.tuning-section` palette —
   no new color tokens needed.
7. Do NOT touch any other tuning section (sliders for the 12 Tier-B
   keys remain unchanged — surgical edit per CLAUDE.md rule 3).
8. Do NOT add a "Cold seams" panel — that's Phase 3.

**Test plan:**
- `tests/test_dashboard_html_renders_seam_toggles.py` (new, ships in
  D3):
  - Parse `index.html` with stdlib `html.parser`; assert exactly 1
    element with `id="hot-seams-section"`.
  - Assert the JS source contains a `fetch("/config/model-swap")` call.
  - Assert no element with `id="cold-seams-section"` (Phase 3 boundary).
- Manual: `python -m manual_test_console.server --use-stubs`; open the
  dashboard; toggle each of the 12 seams; verify the audit tail shows
  `model_swap_completed` rows.

**Success criterion:**
- `pytest tests/test_dashboard_html_renders_seam_toggles.py` passes.
- Manual smoke verifies toggle visual + audit-tail emission.

**Anchors locked:**
- F1 (12 hot seams, order from PR #270 §UI sketch).
- F6 (no cold-seam UI in Phase 1).

**OQs pre-resolved:**
- *OQ: should the toggle be a checkbox, button, or two radios?* —
  **Lean: single toggle button** (`[● on  ○ off]` rendered as one
  clickable element). Matches PR #270 §UI sketch line 365–370 visual.
- *OQ: persist toggle state across page reload?* — **Lean: no
  client-side persistence.** The server's ConfigStore is the source of
  truth; page reload re-fetches `/config/seams`. Matches merged design
  OQ-3 lean.

---

### Task D4 — Live-pipeline factory integration

**Files touched:**
- `manual_test_console/live_pipeline.py` (read `config_store.get_seam(...)`
  at session construction; gate the 12 factory args)
- `manual_test_console/server.py` (mainpath CLI mapping —
  `args.enable_*` booleans into `seam_defaults`)

**Implementation sketch:**
1. In `build_live_pipeline()` at line 474:
   - Where `config_store: ConfigStore | None = None` is already an
     arg, after the existing `use_stubs` block at line 535, add a new
     block that consults the ConfigStore (if provided) and forces each
     factory arg to `None` when the corresponding seam is disabled:
     ```python
     if config_store is not None:
         if not config_store.get_seam("vad"): vad_model = None
         if not config_store.get_seam("smart_turn"): smart_turn_model = None
         if not config_store.get_seam("backchannel"): backchannel_model = None
         if not config_store.get_seam("asr"): asr_model = None
         if not config_store.get_seam("tts"): tts_adapter = None
         if not config_store.get_seam("av_conflict_scorer"): av_conflict_scorer = None
         if not config_store.get_seam("urgency_scorer"): urgency_scorer = None
         if not config_store.get_seam("embedder"): embedder = None
         if not config_store.get_seam("deictic_model"): deictic_model = None
         # scene_scorer, grounding_model, attachment_risk_monitor,
         # fast_tool_dispatcher: gated below at their wiring sites.
     ```
   - This block sits BEFORE the existing `if vad_model is None: vad_model
     = EnergyVADModel()` fallbacks at line 548, so `disabled` cleanly
     becomes "use the existing None-handling neutral path" per F2.
2. For the 3 seams that don't yet flow through `build_live_pipeline()`'s
   parameter list (`scene_scorer`, `grounding_model`,
   `attachment_risk_monitor`, `fast_tool_dispatcher`):
   - `scene_scorer` + `grounding_model` are wired in `server.py` at the
     `VisionSidecar` construction site (line 397–406). Add a similar
     ConfigStore check there.
   - `attachment_risk_monitor` at `live_pipeline.py:607`: gate the
     `late_subscribe` call on `config_store.get_seam(
     "attachment_risk_monitor")`. Default behavior unchanged when
     ConfigStore is absent.
   - `fast_tool_dispatcher` is not yet imported by `live_pipeline.py` as
     of v0.1g — check at D4 implementation time and either gate or
     defer to a follow-up note (do not add new imports speculatively).
3. In `server.py` mainpath (the argparse → `build_app()` mapping),
   construct `seam_defaults` from `args.enable_*` booleans:
   ```python
   seam_defaults = {
       "vad": args.enable_silero_vad,
       "smart_turn": args.enable_pipecat_smart_turn,
       # ... per the 6 existing --enable-* flags from PR #255
   }
   ```
   Pass to `build_app(..., seam_defaults=seam_defaults)`.
4. The `_NullAttachmentRiskMonitor` class: check if a Null variant
   already exists in `companion_harness/attachment_risk_monitor.py`. If
   yes, reuse. If no, add a 10-line `_NullAttachmentRiskMonitor` to
   `live_pipeline.py` (do not create a new module — surgical edit).
5. Do NOT touch any policy or detector code (`speak_policy.py`,
   `turn_detector_*.py`, etc.).
6. Do NOT modify any event-emission code (D2 territory).
7. Do NOT modify any ConfigStore API (D1 territory; only call
   `get_seam`).

**Test plan:**
- `tests/test_live_pipeline_reads_seam_state.py` (new, ships in D4):
  - Construct a ConfigStore with `set_seam("asr", False)`; build a
    live pipeline; assert the ASR detector wired into the orchestrator
    is `EmptyTranscriptASRModel` (the neutral fallback).
  - With all 12 seams enabled (default), the pipeline wires the
    real-or-injected adapters as before (no regression).
  - With `set_seam("attachment_risk_monitor", False)`: pipeline does
    NOT call `late_subscribe(arm.on_event)`.
- Existing `tests/test_live_pipeline_*.py` suite must remain green
  (use_stubs path unaffected; default behavior unaffected when
  config_store is None).

**Success criterion:**
- `pytest tests/test_live_pipeline_reads_seam_state.py` passes.
- `pytest tests/test_live_pipeline_*.py` (existing, ~12 files) no
  regressions.
- `pytest tests/test_server_*.py` no regressions.

**Anchors locked:**
- F2 (`disabled` → factory returns None → existing neutral fallback).
- F4 (ConfigStore is the read source).

**OQs pre-resolved:**
- *OQ: should D4 add a Null variant for fast_tool_dispatcher even though
  it's not yet imported by live_pipeline?* — **Lean: no.** YAGNI per
  CLAUDE.md rule 2. The HTTP route accepts the seam name (D1 listed
  it); the live-pipeline gating is added when the import lands. D5
  test for `fast_tool_dispatcher` asserts the toggle round-trips through
  HTTP + ConfigStore only, not through `build_live_pipeline()`.
- *OQ: should `use_stubs=True` override ConfigStore seam state?* —
  **Lean: yes (existing precedent).** The existing `use_stubs` block at
  line 535 already overrides every factory arg to a stub; the new
  ConfigStore-gating block must run AFTER (so `use_stubs=True` is a
  hard override). Document this in the docstring.

---

### Task D5 — Contract tests

**Files touched:**
- `tests/test_dashboard_model_swap.py` (new — per-seam HTTP round-trip)
- `tests/test_model_swap_events.py` (new — audit completeness + DAG
  closure)
- `tests/test_dashboard_html_renders_seam_toggles.py` (new — already
  ships in D3; D5 expands to assert all 12 seam labels render)
- `tests/test_live_pipeline_reads_seam_state.py` (new — already ships
  in D4; D5 expands to cover all 12 seams)
- `tests/test_hot_swap_latency_p95.py` (new — numeric gate)

**Implementation sketch:**
1. `test_dashboard_model_swap.py`: parametrized test, one case per
   seam in `HOT_SEAMS`. Each case:
   - Starts the manual-test server in stubs mode.
   - `POST /config/model-swap {seam, enabled: False}`; asserts 200 +
     `accepted=True`.
   - `GET /config/seams`; asserts the targeted seam is `enabled:
     False`, others remain `enabled: True`.
   - `POST /config/model-swap {seam, enabled: True}` to revert.
2. `test_model_swap_events.py`:
   - **`model_swap_audit_completeness == 1.0`**: drive 24 swaps (12
     seams × {True, False}); assert event log contains exactly 24
     `model_swap_completed` events and zero `model_swap_rejected`.
   - **DAG closure**: every `model_swap_completed` cites an
     `operator_action` via `caused_by[]`; every `operator_action`
     has empty `caused_by[]`; no orphan events.
   - **Rejected path**: 1 swap with `seam="bogus"`; assert exactly 1
     `model_swap_rejected` with `reason="unknown_seam"`.
3. `test_dashboard_html_renders_seam_toggles.py` (D5 expands D3's
   smoke):
   - Parse `index.html`; assert 12 elements with `class="seam-row"`
     (one per `HOT_SEAMS` entry).
   - Assert each `seam-row` has a `data-seam` attribute matching one of
     the 12 names.
4. `test_live_pipeline_reads_seam_state.py` (D5 expands D4's smoke):
   - Parametrize over the 11 seams that flow through
     `build_live_pipeline()` (skip `fast_tool_dispatcher` per F6-aligned
     OQ). For each: `set_seam(seam, False)`; build pipeline; assert
     the corresponding wired adapter is the neutral fallback.
5. `test_hot_swap_latency_p95.py`:
   - Drive 100 `POST /config/model-swap` calls.
   - Collect `latency_ms` from each `model_swap_completed` event's
     payload.
   - Assert `sorted(latencies)[95] < 100`.
   - Skip with a clear message if the test machine is under load
     (latency is measured per-request, so this should hold trivially
     on any non-overloaded CI).
6. No new files outside `tests/`.

**Test plan:**
- `pytest tests/test_dashboard_model_swap.py
  tests/test_model_swap_events.py
  tests/test_dashboard_html_renders_seam_toggles.py
  tests/test_live_pipeline_reads_seam_state.py
  tests/test_hot_swap_latency_p95.py` all pass.

**Success criterion:**
- All 5 new test files green in canonical venv on b200
  (`/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/`).
- No regressions in the full test suite.
- The three numeric gates from PR #270 are demonstrably met:
  - `model_swap_audit_completeness == 1.0`
  - `hot_swap_latency_ms_p95 < 100`
  - `replay_determinism_after_swap == 1.0` (structural form — see F7)

**Anchors locked:**
- All Phase 1 numeric gates from PR #270 §Numeric gates.

**OQs pre-resolved:**
- *OQ: should D5 ship a `scripts/v0_1h_replay_report.py` per-span
  breakdown?* — **Lean: no, defer to Phase 1.5.** Per F7, the full
  replay-determinism verification requires a callable replay API; that
  ships in a follow-up. Phase 1 contract tests cover the structural
  invariants (audit completeness, DAG closure, latency).

---

## §Dependency graph

```
D1 (API + ConfigStore) ──→ ┬─→ D2 (events) ──┐
                            ├─→ D3 (UI) ──────┤
                            └─→ D4 (pipeline)─┴─→ D5 (contract tests)
```

Linear notation:

> D1 → (D2 ‖ D3 ‖ D4) → D5

**D1 is on the critical path.** D2, D3, D4 fan out 3× ONLY after D1's
PR is on `origin/main` (same discipline as eval Phase A's A2 gate).
D5 cannot dispatch until D2, D3, D4 have all merged because each test
file imports symbols from a different sub-PR (events from D2's emission
path, HTML structure from D3, factory wiring from D4).

If D2/D3/D4 must overlap (e.g., to compress wall-clock): each branches
from D1's pre-merge feature branch and rebases on `main` post-D1-merge.
The simpler discipline is to serialize D1 → fan-out.

---

## §Concurrency map

```
agent-1: D1 (API + ConfigStore + route stubs)
        │
        ▼
        GATE: D2/D3/D4 MUST NOT dispatch before D1's PR is on origin/main
        │
        ├──── agent-2: D2 (event emission) ──────┐
        │                                         │
        ├──── agent-3: D3 (frontend UI) ─────────┤
        │                                         │
        └──── agent-4: D4 (live_pipeline wiring)─┴── fan out 3×
                                                  │
                                                  ▼
                                       agent-5: D5 (contract tests)
```

**Max concurrency: 3 agents** during the D2/D3/D4 fan-out. D1 and D5
each serialize.

**Wall-clock budget (optimistic):** D1 ≈ 1 day, D2/D3/D4 in parallel
≈ 1 day, D5 ≈ 1 day. Phase 1 ships in ~3 days of agent-time.

---

## §Numeric gates

Inherited verbatim from PR #270 §Numeric gates. Each row binds to a D5
contract test:

| Gate | Threshold | D5 test |
|---|---|---|
| `model_swap_audit_completeness` | == 1.0 | `test_model_swap_events.py::test_audit_completeness` |
| `hot_swap_latency_ms_p95` | < 100 | `test_hot_swap_latency_p95.py` |
| `replay_determinism_after_swap` | == 1.0 | `test_model_swap_events.py::test_caused_by_closure` (structural form — full Tier-B-replay verification deferred to Phase 1.5 per F7) |

`cold_restart_caused_by_continuity` is Phase 3 only and not gated in
Phase 1.

---

## §Risks

- **D1 ConfigStore schema split.** Adding `seam_state` next to the
  Tier-B `_state` dict risks breaking the `current_state()` consumers
  (the existing dashboard slider population code). Mitigation: D1
  expands `current_state()` to a `{"values": ..., "seams": ...}` shape
  only in the HTTP response; the in-memory `current_state()` method
  signature is unchanged.
- **D3 optimistic-UI drift.** A failed swap that's only visible via the
  red-flash + audit-tail could leave the UI out of sync if the user
  ignores the flash. Mitigation: the D3 JS subscribes to
  `model_swap_completed` events on the display WS (already used by the
  existing audit-tail click-to-jump) and reconciles whenever a server-
  driven swap arrives.
- **D4 use_stubs precedence.** If the ConfigStore-gating block runs
  BEFORE the existing `use_stubs` block, a `disabled` toggle in
  `--use-stubs` mode could leak a `None` past the stub-installation
  path and crash the orchestrator. Mitigation: the gating block runs
  AFTER `use_stubs` (per F2 / D4 implementation sketch step 1).
- **D5 latency gate flake on overloaded CI.** A `<100ms p95` gate can
  flake on a heavily-loaded CI machine. Mitigation: per implementation
  sketch step 5, the test has a documented skip-with-reason when the
  machine is detectably under load (uptime-based check). The gate is
  load-bearing in the design but tolerance-aware in CI per invariant
  #6.
- **Phase 1 ↔ Phase 3 boundary churn.** If Phase 3 changes the
  cold-seam event shape, D2's `model_swap_*` payload may need
  extension. Mitigation: payload is dict-typed, additive extensions
  are non-breaking. Document this in F3.

---

## §Cross-references

- `docs/plan-dashboard-model-picker-draft.md` (the merged design;
  Phase 1 here = §Phase 1 of that doc).
- `docs/architecture-v0.1.md` Part 2 (invariants #1, #5, #9).
- `manual_test_console/server.py:256–333` — existing
  `_make_operator_action_event` / `_make_config_change_event` pattern
  reused verbatim by D2.
- `manual_test_console/config_store.py` — extended in D1, not
  rewritten.
- `manual_test_console/config_schema.py:47` — Tier-B `ALLOWLIST`
  pattern that D1 mirrors for `HOT_SEAMS` + `validate_seam_patch`.
- `manual_test_console/live_pipeline.py:474` — `build_live_pipeline()`
  factory injection site that D4 gates.
- `manual_test_console/index.html:44` — existing tuning-drawer CSS
  that D3 reuses (no new color tokens).
- `docs/plan-eval-phase-a-execution.md` — concurrency-map convention
  and fan-out-gate discipline this plan mirrors.
- PR #255 — the 6 `--enable-*` opt-in flags whose semantics F5
  preserves.
- PR #270 (merged 2026-05-15) — design doc; Anchors 1–5; numeric gates;
  OQ leans.

---

## §Open questions still unresolved (file as GH issues)

1. **OQ-D1.1** — Should `validate_seam_patch` accept `0` / `1` as
   `enabled` (Python-truthy)? Lean: no (mirror the Tier-B `bool`
   rejection in `validate_patch`). **Action: file as GH issue if
   plan-critic disagrees.**
2. **OQ-D4.1** — Should `fast_tool_dispatcher` ship a Null variant in
   Phase 1 so D5 can exercise its toggle through `build_live_pipeline()`
   instead of HTTP-only? Lean: no (YAGNI; the import hasn't landed in
   `live_pipeline.py`). **Action: file as GH issue.**
3. **OQ-D5.1** — Should the latency p95 gate be measured server-side
   (from event payload) or client-side (HTTP roundtrip)? Lean:
   server-side (server-recorded `latency_ms` excludes client network
   noise; the merged design's "operator-perceived snappy" framing is
   weakly violated, but the alternative requires a real browser).
   **Action: file as GH issue.**

No other OQs require external decisions. All other "OQ" entries above
are pre-resolved with documented leans.

---

## §Critical findings

1. **No new tier in the Tier-A/B/C taxonomy.** The merged design's
   "Tier D" is a conceptual label, not a new code-level distinction in
   Phase 1. The seam-state namespace lives next to Tier-B inside the
   same ConfigStore. A "Tier D" `_TIER_D_KEYS` frozenset is **not**
   added in Phase 1; the seam set is enumerated in `HOT_SEAMS`
   directly. Tier-D-as-code lands when Phase 2 adds multi-impl adapter
   identity (`real_v2`, `real_v3`).
2. **`fast_tool_dispatcher` is not yet wired into
   `live_pipeline.py`.** Confirmed by `grep -n
   "fast_tool_dispatcher\|FastToolDispatcher" live_pipeline.py` (zero
   hits as of v0.1g HEAD). D1 includes it in `HOT_SEAMS` so the route
   accepts the name; D4 does NOT wire it through the factory. D5's
   live-pipeline test skips it (parametrize over the 11 wired seams).
   When `fast_tool_dispatcher` lands in `live_pipeline.py` as part of a
   future PR, the gating block in D4 gains one more line — that's the
   only churn.
3. **`attachment_risk_monitor` is unconditionally subscribed today.**
   `live_pipeline.py:607` calls `arm = EventStreamAttachmentRiskMonitor()`
   and `shielded_logger.late_subscribe(arm.on_event)` without a
   condition. D4 wraps this in a `config_store.get_seam(
   "attachment_risk_monitor")` check (default True, so behavior is
   unchanged when ConfigStore is None or the seam is enabled).
4. **`scene_scorer` + `grounding_model` are wired in `server.py`, not
   `live_pipeline.py`.** They're constructed at vision-sidecar
   construction (server.py:397–406), not inside the factory. D4 gates
   them at that site. This is the only D4 file-touch in `server.py`,
   and it's surgical (one `if` per seam).
5. **Canonical venv requirement (MEMORY.md).** D5's test suite runs
   under `/raid/yid042/venvs/companion-harness/bin/python3 -m pytest`
   per project convention. Bare `python3` lacks the aiohttp + torch
   stack and would skip or silently pass.

---

**End of plan.** Sign-off: pending plan-critic review (Phase 1
dispatch).
