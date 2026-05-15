# ADR: DecisionTrace production wiring deferred to Stage-4 activation

**Status:** deferred  
**Tracking issue:** [#34](https://github.com/yid042/companion-agent-harness/issues/34)  
**Date:** 2026-05-14

## Context

`DecisionTrace` is defined in `companion_harness/schemas.py` and is never
instantiated anywhere in the codebase. `SpeakPolicy.decide()` returns a
`SpeakDecision`; no `DecisionTrace` is co-emitted.

This is correct for the current milestone. A spec-researcher pass (see issue
#34) established:

- The spec does not assign a stage to `DecisionTrace` production.
- The spec says `DecisionTrace`'s fields are "sufficient for Tier B policy
  replay" — sufficient, not required. Satisfying Stage 0's
  `test_policy_replay_exact` with `SpeakDecision` alone is a permitted
  implementation choice.
- The first stage where `DecisionTrace` is genuinely load-bearing is **Stage 4
  (Memory)** — the `test_why_did_you_say_that` contract test expects a
  retrievable policy-decision trace.

## Decision

`DecisionTrace` production wiring is **deferred to the Stage-4 activation
milestone**. No action is taken in this scaffolding pass.

Specifically, the following are **Stage-4 activation work**, not this pass:

- No `decision_trace_builder.py` module is added.
- No `co_emit_decision_trace` flag is added (on `SpeakPolicy`, in a config
  record, or as a module constant).
- No unit test for `DecisionTrace` instantiation or co-emission is added.
- `SpeakPolicy.decide()` is not modified.

## What Stage-4 activation must wire

When Stage 4 (Memory) work begins:

1. `SpeakPolicy.decide()` co-emits a `DecisionTrace` alongside the
   `SpeakDecision`, linked by a shared `decision_id`.
2. `DecisionTrace` carries: `input_event_ids`, `signal_event_ids`,
   `threshold_path`, `counterfactuals`, `policy_version`, `config_version`,
   `model_adapter_versions`.
3. `SpeakDecision` and `DecisionTrace` are complementary — `SpeakDecision` is
   the live action directive; `DecisionTrace` is the durable audit record for
   Tier B replay and Stage 4 explainability.

The location of the `co_emit_decision_trace` flag (on `SpeakPolicy` vs. an
implementation-config record) is an open question deferred to Stage-4 planning
(see roadmap-stage34-scaffolding-draft.md Open Question 2).
