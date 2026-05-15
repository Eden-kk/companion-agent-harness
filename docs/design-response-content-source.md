# Design — `SpeakDecision.response_content_source`

> P0 follow-up to GPT's PR #144 review. The current grace-window-then-drop path
> in `_synthesis_dispatch_task` conflates "policy approved speech" with
> "Thinker had a proposal ready". For Stage 6 aesthetic reactions that is
> correct. For Stage 1/3 direct-response action types it produces a silent
> failure mode where policy approves a `full_response`, the foreground proposal
> stream is slightly slow, the grace window expires, the gate emits
> `synthesis_skipped_no_proposal`, and the agent goes silent on a user-addressed
> turn. Honest, but a content-sourcing bug masquerading as a policy decision.
>
> No code in this PR. Design only.

---

## §1 — The current problem

### Code path

`companion_harness/realtime_orchestrator.py:630-687` — `_synthesis_dispatch_task`
(T4 in the orchestrator's four-task graph; `realtime_orchestrator.py:154`).

The relevant gate is `realtime_orchestrator.py:644-655`:

```python
# Grace window: wait for at least one proposal BEFORE snapshot (edge case h).
if not self.proposal_buffer:
    try:
        await asyncio.wait_for(
            self._first_proposal_event.wait(),
            timeout=self._proposal_batch_window_ms / 1000.0,
        )
    except asyncio.TimeoutError:
        self._emit("synthesis_skipped_no_proposal", [policy_evt_id], "signal")
        self.proposal_buffer.clear()
        self._first_proposal_event.clear()
        continue
```

The window is `proposal_batch_window_ms`:
- Adapter default: `80` ms (`realtime_orchestrator.py:175`).
- Live manual-test pipeline default: `200` ms
  (`manual_test_console/live_pipeline.py:312`, `:413`).
- Tier-B config-exposed key: `orchestrator.proposal_batch_window_ms`
  (`manual_test_console/config_schema.py:136-137`).

The gate is one-size-fits-all. Every approved action — `full_response`,
`clarification`, `short_reaction`, `alert`, `tool_status`,
`aesthetic_reaction`, `backchannel` — passes through the same `await
self._first_proposal_event.wait()` regardless of whether the action's content
actually needs a `ThinkerProposal` to exist.

### Why this matters

Invariant #2 ("no direct Thinker speech") says proposals flow through the
policy layer. It does **not** say every action_type sources its content from
a Thinker proposal. The orchestrator currently behaves as if it does.

For Stage 1/3 the bulk of `full_response` content SHOULD come from foreground
proposals — MiniCPM-o streams response text alongside its audio understanding,
and those tokens are what T3 (`_foreground_stream_task` at
`realtime_orchestrator.py:571-584`) appends to `proposal_buffer`. But this is
an implementation property of the current foreground model, not an
architectural invariant. The architecture has to distinguish actions that
genuinely need a proposal (aesthetic reactions, observation-class proactive
turns) from actions whose content has other sources:

- `backchannel` content is a fixed template ("mm-hmm" / "uh-huh"). No proposal
  needed.
- `alert` content is a per-event template ("Wait — the water's boiling over.";
  spec lines 619-621). No proposal needed.
- `tool_status` content is sourced from `tool_progress_evidence`
  (`schemas.py:163`, Stage 5 / v0.1f Anchor 4). No Thinker proposal needed.
- `full_response` / `clarification` content needs a foreground proposal but
  cannot tolerate the proposal being absent — silence on a user-addressed turn
  is a regression.
- `aesthetic_reaction` content needs a Thinker proposal AND silently dropping
  without one is the correct behavior (Stage 6: aesthetic without grounded
  content collapses to silence per spec lines 627-639).

### Concrete failure mode

1. User finishes a direct question; T1+T2 confirm EOU; `decide()` returns
   `action_type="full_response"`.
2. T3 hands the batch to MiniCPM-o; first-token latency is ~250 ms on b200
   under realistic load.
3. T4 grace window (200 ms in live config) expires.
4. `synthesis_skipped_no_proposal` event is emitted; `proposal_buffer` is
   cleared; `_first_proposal_event` is cleared; T4 `continue`s.
5. T3's proposal tokens arrive ~50 ms later but `proposal_buffer` was already
   cleared and T3 will append into it for a batch that is no longer being
   waited on.
6. From the user's perspective: silence. The agent did not respond.

The replay trace is honest — there is a `policy_decision`, a
`synthesis_skipped_no_proposal`, no `assistant_generation_start`, no
`assistant_audio_buffer_queued`. The event DAG closes. Invariant #1 holds.
But the product behavior is wrong: a `full_response` decision did not become
voice, and the cause was timing, not policy.

---

## §2 — Proposed: `SpeakDecision.response_content_source`

A new enum field on `SpeakDecision` (`schemas.py:168-179`) that names where
the response text comes from. T4 consults this field — not the action_type
alone — to decide which content-sourcing path to take and which fallback
strategy applies on timeout.

```python
response_content_source: Literal[
    "foreground_response_proposal",  # full_response / clarification / short_reaction
    "thinker_proposal",              # aesthetic_reaction
    "template_backchannel",          # backchannel ("mm-hmm" / "uh-huh")
    "template_alert",                # alert (per-event template)
    "template_tool_status",          # tool_status (tool_progress_evidence string)
    "no_synthesis",                  # silence
]
```

The new field is a peer of `action_type`. They are not redundant: the
action_type defines what kind of turn it is (silence vs short reaction vs full
response vs aesthetic). The response_content_source defines where the words
come from for that turn. The matrix below shows the per-action defaults but
the field is durable metadata — once stamped on a logged `SpeakDecision`, it
explains in replay why content sourcing took the path it did.

---

## §3 — Per-action defaults

| `action_type`        | `response_content_source` default | Hard-fail-to-silence allowed?       |
|----------------------|-----------------------------------|--------------------------------------|
| `silence`            | `no_synthesis`                    | n/a                                  |
| `backchannel`        | `template_backchannel`            | never fails; fixed template          |
| `short_reaction`     | `foreground_response_proposal`    | extended grace; see §4               |
| `full_response`      | `foreground_response_proposal`    | NEVER hard-fail; extended grace + loud log; see §4 |
| `clarification`      | `foreground_response_proposal`    | same as `full_response`              |
| `alert`              | `template_alert`                  | never fails on proposal              |
| `tool_status`        | `template_tool_status`            | sourced from `tool_progress_evidence`; no Thinker dependency |
| `aesthetic_reaction` | `thinker_proposal`                | CAN hard-fail; silent drop is correct per Stage 6 |

The architectural claim: **proposal-required-and-hard-failing actions are a
strict subset of approved-speech actions**. Only `aesthetic_reaction`
SHOULD silently drop on missing proposal. Everything else needs a fallback.

This collapses two distinct event classes that today share the
`synthesis_skipped_no_proposal` event_type:

- `aesthetic_reaction` + no proposal = intentional drop. The action exists for
  Stage 6 texture; silence is the design.
- `full_response` + no proposal = failure mode. The action exists because
  policy decided the user is owed a reply; silence is a bug.

Conflating these in one event_type makes the replay log lie about which
category of event is happening.

---

## §4 — Fallback strategy for `foreground_response_proposal`

Today (`realtime_orchestrator.py:651-655`): grace window expires →
`synthesis_skipped_no_proposal` → silent. GPT calls this "policy approved
speech but no proposal arrived, so no voice."

Three candidate fixes:

**Option A (preferred): per-action grace windows.** Allow
`proposal_batch_window_ms` to be a lookup keyed on
`response_content_source` (or on `action_type`, which would be equivalent
under the §3 mapping). Current 200 ms live default is too tight given
MiniCPM-o's first-token latency on b200. Recommendation:
- `full_response` / `clarification`: 800–1200 ms.
- `short_reaction`: 200 ms (unchanged).
- `aesthetic_reaction`: 200 ms (unchanged; hard-fail is acceptable).
- Template-sourced actions: 0 ms (no wait; synthesize template immediately).

**Option B: hold-please template fallback.** On grace timeout for
`full_response`, synthesize a short acknowledgment ("Let me think.") and
continue waiting for the proposal stream, concatenating proposal text once
it arrives. Higher implementation cost; introduces a Stage 6-adjacent
template content concern (see §9). Defer to v0.2.

**Option C: fail-loud.** Keep current behavior but split the event_type so
operators can see this is a content-sourcing failure rather than a policy
decision. Emit a `synthesis_silenced_proposal_starved` event (distinct from
the aesthetic-drop `synthesis_skipped_no_proposal`) referencing the
upstream `SpeakDecision` and the `response_content_source` field.

**Recommendation: A + C in Phase 1; defer B to v0.2.** Per-action grace
windows handle the common case (MiniCPM-o first-token latency under load);
the fail-loud event records when even the extended grace is exhausted so
the operator can see the failure rather than experiencing a silent agent
and inferring it from the trace.

---

## §5 — Schema change

`companion_harness/schemas.py:168-179` — extend `SpeakDecision`:

```python
response_content_source: Literal[
    "foreground_response_proposal",
    "thinker_proposal",
    "template_backchannel",
    "template_alert",
    "template_tool_status",
    "no_synthesis",
] = "foreground_response_proposal"
```

Default `"foreground_response_proposal"` preserves the current behavior for
any caller that constructs a `SpeakDecision` without setting the field. The
canonical population path is `speak_policy.py:decide()`
(`speak_policy.py:33-184`), which would set the field per the §3 table on
each return.

Field is stable enum-only metadata — same discipline as `primary_reason_code`
(`reason_codes.py`). No free text. Safe for Tier B replay and for
`DecisionTrace.counterfactuals` augmentation.

---

## §6 — Per-action grace window

A new module-level mapping or per-instance config:

```python
PROPOSAL_GRACE_MS: dict[str, int] = {
    "full_response":     1200,
    "clarification":     1200,
    "short_reaction":     200,
    "aesthetic_reaction": 200,
}
# Template-sourced actions (backchannel / alert / tool_status) bypass the
# grace path entirely — they take the synthesize-immediately branch.
```

Per PR #143's Tier-B dashboard work, these belong as Tier-B config keys
(replay-safe; tunable per session) rather than hard-coded constants. The
existing `orchestrator.proposal_batch_window_ms` becomes a back-compat
fallback when no per-action override is set.

`_synthesis_dispatch_task` (`realtime_orchestrator.py:630-687`) consults the
new field instead of `self._proposal_batch_window_ms` unconditionally:

```python
# Pseudocode for the gate at realtime_orchestrator.py:644-655:
match decision.response_content_source:
    case "template_backchannel" | "template_alert" | "template_tool_status":
        # No proposal wait; synthesize template text directly.
        text = _template_text_for(decision)
    case "thinker_proposal":
        # Existing hard-fail-to-silence behavior on grace timeout.
        # Continues to emit synthesis_skipped_no_proposal.
        ...
    case "foreground_response_proposal":
        # Extended grace; on timeout, emit synthesis_silenced_proposal_starved
        # and (Phase 1) fall through to silence with the louder event.
        ...
```

---

## §7 — Event log changes

Today's `synthesis_skipped_no_proposal` is overloaded. Split it:

- `synthesis_skipped_no_proposal` — narrowed to `response_content_source =
  thinker_proposal` (aesthetic_reaction only). The intentional drop. Stage 6
  invariant: aesthetic without content is silence.
- `synthesis_silenced_proposal_starved` — new event_type, fired when
  `response_content_source = foreground_response_proposal` AND the per-action
  grace window expires AND no proposal arrived. This is the failure mode.

Both events MUST reference the upstream `SpeakDecision` via `caused_by[]`
(already done — `realtime_orchestrator.py:652` cites `policy_evt_id`). Both
SHOULD carry `response_content_source` on the inline payload so the replay
trace explains which class of event it is without dereferencing the upstream
decision.

The narrower `synthesis_skipped_no_proposal` continues to count as
expected-and-correct behavior in eval reports. The new
`synthesis_silenced_proposal_starved` counts as a failure indicator — its
count is a new latency / availability metric for the live loop.

Classification axes (per `v0_1f_event_schema.py` discipline):
- `payload_kind`: `"signal"`.
- `subject_class`: `"self"`.
- `sensitivity`: `"safe"`.
- `retention_policy_id`: same audit class as existing `policy_decision` events
  (see `v0_1e_event_schema.py`).
- Required fields: `decision_id`, `action_type`, `response_content_source`,
  `grace_window_ms_used`.

---

## §8 — Implementation plan

### Phase 1 — single PR

Phase 1 is one PR matching CLAUDE.md "one PR, one outcome":

1. Schema: add `response_content_source` field to `SpeakDecision`
   (`schemas.py:168-179`).
2. Policy: populate the field in `speak_policy.py:decide()` per the §3 table.
3. Orchestrator: per-action grace windows in `_synthesis_dispatch_task`
   (`realtime_orchestrator.py:630-687`). Template-sourced actions take a new
   synthesize-immediately branch; foreground-proposal actions get extended
   grace; aesthetic_reaction keeps existing behavior.
4. Event schema: register `synthesis_silenced_proposal_starved` in
   `v0_1f_event_schema.py` (or wherever the live-loop event schemas
   consolidate; see Anchor 3 of v0.1f).
5. Tier-B config: expose per-action grace as a config key per PR #143
   conventions.

Contract tests (Stage 3 / live-loop):
- `test_full_response_uses_extended_grace` — asserts grace window for
  `action_type=full_response` is the extended value, not 200 ms.
- `test_aesthetic_reaction_silently_drops_without_proposal` — asserts the
  existing aesthetic-drop behavior is preserved (`synthesis_skipped_no_proposal`
  still fires for `response_content_source = thinker_proposal`).
- `test_full_response_starvation_emits_silenced_event` — asserts the new
  `synthesis_silenced_proposal_starved` event fires when a `full_response`
  decision is starved after the extended grace.
- `test_backchannel_bypasses_grace_window` — asserts that
  `template_backchannel` synthesis does not block on `_first_proposal_event`.

### Phase 2 — deferred

- Hold-please template fallback (Option B from §4).
- Templated content for `template_alert` / `template_tool_status` —
  per-event-class string tables; needs alignment with the prosody and
  companion-texture work.
- Multi-source content (Thinker proposal merged with foreground response
  proposal for the same turn).

---

## §9 — Out of scope

- The actual template strings ("Let me think.", "mm-hmm") — deferred to a
  Stage 6 companion-texture PR. This design only carves out the
  classification slot.
- Vision-derived response content (multimodal proposals) — orthogonal;
  VisionSidecar wiring handles the input side, foreground model handles the
  proposal side. The response_content_source field does not enumerate the
  modality of the proposal source.
- ASR-driven "is the user still talking?" mid-response detection — orthogonal;
  that's barge-in scope (`_fire_barge_in` at `realtime_orchestrator.py:715`).
- Latency budget recalibration. Spec Part 8 caps `direct_question_latency_p50
  < 800 ms` and `p95 < 1500 ms`; this design proposes 1200 ms grace which
  consumes most of the p95 budget. Empirical recalibration of MiniCPM-o
  first-token latency on b200 is a separate measurement PR.

---

## §10 — Open questions for project-lead decision

- **OQ-1**: How long is "too long" for the foreground grace window? 1200 ms
  is a starting guess; spec Part 8 line 911-912 caps
  `direct_question_latency_p50 < 800 ms` and `p95 < 1500 ms`. The grace
  window IS part of that latency budget. Setting grace to 1200 ms means we
  can use most of the 1500 ms p95 budget for synthesis. Lower values trade
  reliability for latency margin.
- **OQ-2**: Should `synthesis_silenced_proposal_starved` block speech and
  fall through to silence (current proposal — matches existing behavior,
  just better-named), or fall through to a hold-please template (Option B
  from §4)? Option B is the more product-correct behavior but pulls Stage 6
  template content into Stage 3 scope.
- **OQ-3**: Per-action constant vs per-action multiplier on a single base
  window? Two equivalent expressions:
  - `PROPOSAL_GRACE_MS = {"full_response": 1200, ...}` (per-action constant).
  - `PROPOSAL_GRACE_MS = base_window * {"full_response": 6.0, ...}` (per-action
    multiplier of a single base knob).
  The multiplier form is easier to recalibrate globally; the constant form is
  more explicit in trace logs. Recommendation: constant; trace clarity wins
  over operator convenience.
- **OQ-4**: Should `tool_status` template sourcing assume
  `tool_progress_evidence` is always present in `PolicyInputs` when
  `action_type=tool_status` is selected? `speak_policy.py:decide()` does not
  currently gate on `tool_progress_evidence`; v0.1f Anchor 4 leaves this for
  Stage 5 wiring. Phase 1 should assume yes and add an assertion at the T4
  synthesis branch; Stage 5 will be responsible for ensuring the invariant
  holds upstream.
- **OQ-5**: Backward compat — does the default value on the new field
  (`"foreground_response_proposal"`) break any existing `SpeakDecision`
  consumer? Spot-check: `DecisionTrace.build_decision_trace`
  (`speak_policy.py:263`) does not read content_source; existing event
  consumers don't either. Safe default. But the live config dashboard (PR
  #143) will need a row.
