# Plan — Real `BackgroundReasoner` (smart-path tool routing) — DRAFT

## Status

**DRAFT — 2026-05-15.** Replaces `FakeBackgroundReasoner` placeholder from v0.1f Task 8. Implements the smart-path tool router that the spec defers to "v0.1f+1".

## Why

v0.1f shipped two tool-routing paths: the **fast path** (`FastToolDispatcher`, local-tool registry, deterministic SHA-256 `tool_call_id`) and the **smart path** (`FakeBackgroundReasoner`, deterministic fake). The fast path handles known local tools (memory lookups, time, weather stub). The smart path is supposed to handle *novel* multi-step tool reasoning: "find a recipe that uses what's in the fridge and tell me if I'm missing anything."

Today the smart path returns canned outputs. Real LLM-driven (or MCP-driven) reasoning is needed.

## Goal

Replace `FakeBackgroundReasoner` with a real backend behind the existing `BackgroundReasoner` Protocol seam. Same signatures (`select_and_call`, `summarize`), same event emissions (`tool_call_*` chain), same orchestrator wiring — only the implementation changes.

## Anchor decisions — non-transient

### Anchor 1 — Backend choice: MCP server primary, LLM-direct fallback

Use **MCP (Model Context Protocol)** as the canonical backend. MCP is purpose-built for tool routing: discovery, schema-typed calls, structured results.

- **Primary**: `MCPBackgroundReasoner` connecting to a local MCP server.
- **Fallback**: `LLMBackgroundReasoner` calling MiniCPM-o.chat() directly with a tool-routing prompt. Used when MCP server is unavailable.
- **Selection**: env var `BACKGROUND_REASONER=mcp|llm|fake`; default `fake` preserves current behavior.

**Rejected:** custom orchestration framework (LangChain, LlamaIndex, etc). MCP is the spec-aligned choice and keeps the abstraction surface minimal.

### Anchor 2 — Event-stream-first: reasoner yields events, never returns batched results

Existing signature `select_and_call(request: ToolDispatchRequest) -> AsyncIterator[Event]` is preserved. The reasoner yields `tool_call_requested → tool_call_dispatched → tool_progress_event* → tool_call_completed | tool_call_cancelled` for each tool it calls (potentially multiple per request). The orchestrator forwards each event to `EventLogger.log()`.

**Replay invariant:** the event log records every call the reasoner made. Tier-B replay reconstructs the policy decisions from the event log alone. The reasoner's internal LLM reasoning is opaque to replay; only its tool-call effects matter.

**Tier-B replay detail (invariant #5):** During Tier-B replay the reasoner is NOT re-run. Recorded `tool_call_completed` events are fed directly from the log back into `ToolProgressEmitter.evidence_at()`, which reconstructs `PolicyInputs.tool_progress_evidence` without invoking the reasoner. This is what preserves determinism: the policy layer sees identical evidence on replay because it reads from the recorded log, not from a live LLM call.

**`routing_tier` metadata (v0.1f Anchor 3 alignment):** When `LLMBackgroundReasoner` (or `MCPBackgroundReasoner`) selects a tool, the `tool_call_dispatched` event is stamped `routing_tier="smart"`, NOT `"fast"`. The fast-vs-smart distinction reflects who chose the tool — local registry decision = `"fast"`, LLM/MCP decision = `"smart"`. `FastToolDispatcher` is the executor; it may be used on either path. The `routing_tier` field records the originating decision path, not which executor ran.

### Anchor 3 — Bounded budget per request

Wall-clock budget (e.g., 30 seconds total per `select_and_call`) AND step budget (e.g., max 5 tool calls per request). Both enforced by the reasoner itself; OOB calls raise `BackgroundReasonerBudgetExhausted`. Budget values come from `ConfigStore` (Tier-B knob).

**Why both:** wall-clock catches hangs, step-count catches runaway recursion.

### Anchor 4 — `set_context()` injection for downstream summary

Per v0.1f OQ-7: smart-path summary injection reuses `set_context()` on the foreground model. No new Protocol method. `summarize(results) -> list[MemoryItem]` returns memory items the orchestrator then calls `set_context()` with.

## Open questions (leans)

- **OQ-1**: MCP server discovery — env var `MCP_SERVER_URL` or auto-discover via well-known endpoint? **Lean**: env var only at v0.1f+1; auto-discovery later.
- **OQ-2**: Per-tool retry on transient error? **Lean**: NO at v0.1f+1; one shot per call, fail loud.
- **OQ-3**: Streaming partial results back during multi-step? **Lean**: NO — each tool call is atomic; reasoner yields one progress event per step but doesn't stream within-step.
- **OQ-4**: LLM choice for `LLMBackgroundReasoner` fallback — MiniCPM-o.chat() or a smaller model (Llama 3.2 1B)? **Lean**: MiniCPM-o reuse (already loaded, zero VRAM cost, same pattern as MiniCPMAddressingClassifierImpl). Note: spec Part 9 names Nemotron-3 Nano Omni as the BackgroundReasoner. Using MiniCPM-o here is a pragmatic deviation (reuse already-loaded model); revisit if Nemotron weights become locally available.
- **OQ-5**: How to handle MCP server returning a tool that isn't in the local registry? **Lean**: log `signal_producer_fallback` event with reason `unknown_mcp_tool`, fail the call gracefully, let policy see `tool_status=failed`.

## Tasks (sketch, 8 tasks)

### Wave 1 — Protocol + MCP backend + budget enforcement
- **T1**: Add `mcp>=1.0` to `requirements-b200.txt` (and `requirements-dev.txt` if a mocked MCP server is needed for unit tests).
- **T2**: `MCPBackgroundReasoner(BackgroundReasoner)` — implements `select_and_call` by translating user-utterance + context into MCP tool calls. `MCPBackgroundReasoner` directly wraps the `mcp` Python SDK client inline; no intermediate Protocol layer.
- **T4**: Budget enforcement: `BackgroundReasonerBudgetExhausted(Exception)` is a module-level definition. Each concrete reasoner (MCP first, LLM-direct later) holds a `_budget_remaining` counter and raises this exception inline. NO new base class — keep the Protocol-only seam. Wall-clock + step count enforced per reasoner instance.

### Wave 2 (follow-on PR, out of scope for this PR) — LLM-direct fallback
- **T3**: `LLMBackgroundReasoner(BackgroundReasoner)` — calls MiniCPM-o.chat() with a tool-routing prompt that emits structured tool-call JSON. Parser extracts tool calls; orchestrator dispatches via `FastToolDispatcher` per spec. Ships with its own contract test suite.

### Wave 3 — Config knobs
- **T5**: ConfigStore Tier-B knobs: `reasoner.budget_wall_clock_s`, `reasoner.budget_step_count`.

### Wave 4 — Wiring + selection
- **T6**: `BACKGROUND_REASONER` env var dispatcher in `manual_test_console/server.py`. Defaults to `fake`. If operators later need a CLI flag, it's one line to add — defer until requested. The env-var dispatcher in `server.py` must call adapter constructors only; do NOT import the `mcp` SDK directly in `server.py`. The SDK import lives inside `MCPBackgroundReasoner`.

### Wave 5 — Contract tests
- **T8**: Real backends emit the v0.1f Anchor 2 chain (`tool_call_requested → tool_call_dispatched → tool_progress_event* → tool_call_completed|tool_call_cancelled`), NOT new event types. Verify against `test_v0_1f_event_schema.py`.
  - `test_mcp_reasoner_emits_correct_event_chain`
  - `test_llm_reasoner_parses_tool_calls`
  - `test_budget_wall_clock_exhaustion`
  - `test_budget_step_count_exhaustion`
  - `test_summarize_returns_memory_items_for_set_context_injection`
  - `test_summarize_memoryitem_source_event_id_is_logged_event` — asserts that every `MemoryItem.source_event_id` returned from `summarize()` corresponds to a `tool_call_completed` event_id present in the EventLogger's recorded events (invariant #3)
  - `test_unknown_mcp_tool_emits_signal_producer_fallback`

## Numeric gates (additive to v0.1f gates)

| Metric | Gate | Measurement |
|---|---|---|
| `background_reasoner_budget_exhaustion_rate` | < 5% (tolerable) | Metric: incremented by `BackgroundReasonerBudgetExhausted` raises on `tool_call_cancelled` events with `reason=budget_exhausted`; aggregated via the live-loop metrics pattern established in v0.1f (TBD exact module). |
| `background_reasoner_summary_set_context_attribution_rate` | == 1.0 (every summary reaches foreground) | Metric: ratio of `summarize()` calls that produce at least one `MemoryItem` passed to `set_context()`, recorded on the orchestrator path; TBD via the live-loop metrics pattern established in v0.1f. |
| `signal_producer_fallback_rate_reasoner` | < 1% (MCP server should be reliable) | Metric: incremented by `signal_producer_fallback` events with `source=background_reasoner`; aggregated via the live-loop metrics pattern established in v0.1f (TBD exact module). |

## Risks

1. **MCP ecosystem maturity**: MCP is newish (2024). Server quality varies. Mitigation: scope this PR to MCP-primary only; LLM-direct fallback ships as a separate follow-on PR with its own contract test suite. Shipping two partial implementations in one PR is higher risk than one complete one.
2. **MiniCPM-o tool-routing prompt drift**: LLM-extracted tool calls may not match the local registry's strict schema. Mitigation: defensive parser, fail-loud with `signal_producer_fallback` rather than silently invent calls.
3. **Budget tuning**: 30s wall-clock + 5 steps may be too tight or loose. Mitigation: ConfigStore Tier-B knobs allow live tuning.

## Out of scope

- Recursive reasoner-calling-reasoner (yagni at v0.1f+1).
- Custom MCP server implementation (use existing ecosystem).
- Multi-model reasoner ensembles.

## Cross-references

- `docs/roadmap-v0.1f-draft.md` — v0.1f Task 8 (FakeBackgroundReasoner shipped) and Task 9 (smart-path orchestrator wiring).
- `docs/architecture-v0.1.md` — Stage 5 background reasoning section.
- MCP spec: https://modelcontextprotocol.io
- `companion_harness/background_reasoner.py` — existing `BackgroundReasoner` Protocol + `FakeBackgroundReasoner`.
