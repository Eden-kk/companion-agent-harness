# ADR: `tool_status` action type wiring deferred to Stage-5 activation

**Status:** deferred  
**Date:** 2026-05-14

## Context

`tool_status` is present in the `SpeakDecision.action_type` Literal
(`companion_harness/schemas.py`) but is not handled in any branch of
`SpeakPolicy.decide()`. No code path in v0.1d produces a `SpeakDecision` with
`action_type == "tool_status"`.

Wiring `tool_status` now would violate spec invariant #9:

> **No invented tool progress.** Foreground narration about tool progress
> requires a corresponding `ToolProgressEvent`. Invented progress in voice is
> more manipulative than in text.

v0.1d / Stage-3 has no `ToolProgressEvent` event type and no tool-dispatching
infrastructure. Emitting a `tool_status` action without an anchoring
`ToolProgressEvent` in the event DAG is a direct violation of invariant #9.

## Decision

`tool_status` wiring is **deferred to the Stage-5 (Background reasoning &
tool routing) activation milestone**. The Literal entry is retained as a
reserved slot; `SpeakPolicy.decide()` is not modified.

## What Stage-5 activation must build alongside `tool_status` wiring

1. **`ToolProgressEvent` event type** — a first-class event in the event DAG
   that carries `tool_id`, `progress_fraction`, `status_text`, and `caused_by`.
   Every `tool_status` `SpeakDecision` must reference a `ToolProgressEvent` in
   its `caused_by` list; orphan `tool_status` actions fail Stage-0 DAG checks.

2. **`ToolDispatcher` adapter tool-progress emission contract** — the adapter
   that routes tool calls must emit `ToolProgressEvent` at defined checkpoints
   (at minimum: on start, on completion, on error). `SpeakPolicy` consumes these
   events as signals; it never reads tool state directly.

3. **`SpeakPolicy.decide()` branch for `tool_status`** — a policy branch that
   gates `tool_status` speech on a live `ToolProgressEvent` in the causal window,
   subject to the existing proactivity budget and silence-wins-ties invariant
   (invariant #8).
