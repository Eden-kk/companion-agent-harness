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

### Anchor 3 — Bounded budget per request

Wall-clock budget (e.g., 30 seconds total per `select_and_call`) AND step budget (e.g., max 5 tool calls per request). Both enforced by the reasoner itself; OOB calls raise `BackgroundReasonerBudgetExhausted`. Budget values come from `ConfigStore` (Tier-B knob).

**Why both:** wall-clock catches hangs, step-count catches runaway recursion.

### Anchor 4 — `set_context()` injection for downstream summary

Per v0.1f OQ-7: smart-path summary injection reuses `set_context()` on the foreground model. No new Protocol method. `summarize(results) -> list[MemoryItem]` returns memory items the orchestrator then calls `set_context()` with.

## Open questions (leans)

- **OQ-1**: MCP server discovery — env var `MCP_SERVER_URL` or auto-discover via well-known endpoint? **Lean**: env var only at v0.1f+1; auto-discovery later.
- **OQ-2**: Per-tool retry on transient error? **Lean**: NO at v0.1f+1; one shot per call, fail loud.
- **OQ-3**: Streaming partial results back during multi-step? **Lean**: NO — each tool call is atomic; reasoner yields one progress event per step but doesn't stream within-step.
- **OQ-4**: LLM choice for `LLMBackgroundReasoner` fallback — MiniCPM-o.chat() or a smaller model (Llama 3.2 1B)? **Lean**: MiniCPM-o reuse (already loaded, zero VRAM cost, same pattern as MiniCPMAddressingClassifierImpl).
- **OQ-5**: How to handle MCP server returning a tool that isn't in the local registry? **Lean**: log `signal_producer_fallback` event with reason `unknown_mcp_tool`, fail the call gracefully, let policy see `tool_status=failed`.

## Tasks (sketch, 8 tasks)

### Wave 1 — Protocol + MCP backend
- **T1**: `MCPClient` Protocol + concrete adapter using `mcp` Python SDK. Mock for unit tests.
- **T2**: `MCPBackgroundReasoner(BackgroundReasoner)` — implements `select_and_call` by translating user-utterance + context into MCP tool calls.

### Wave 2 — LLM fallback
- **T3**: `LLMBackgroundReasoner(BackgroundReasoner)` — calls MiniCPM-o.chat() with a tool-routing prompt that emits structured tool-call JSON. Parser extracts tool calls; orchestrator dispatches via `FastToolDispatcher` per spec.

### Wave 3 — Budget enforcement
- **T4**: Budget state machine on `BackgroundReasoner` base class. Wall-clock + step count. `BackgroundReasonerBudgetExhausted` exception type.
- **T5**: ConfigStore Tier-B knobs: `reasoner.budget_wall_clock_s`, `reasoner.budget_step_count`.

### Wave 4 — Wiring + selection
- **T6**: `BACKGROUND_REASONER` env var dispatcher in `manual_test_console/server.py`. Defaults to `fake`.
- **T7**: `--background-reasoner {fake,mcp,llm}` CLI flag overriding env.

### Wave 5 — Contract tests
- **T8**: 
  - `test_mcp_reasoner_emits_correct_event_chain`
  - `test_llm_reasoner_parses_tool_calls`
  - `test_budget_wall_clock_exhaustion`
  - `test_budget_step_count_exhaustion`
  - `test_summarize_returns_memory_items_for_set_context_injection`
  - `test_unknown_mcp_tool_emits_signal_producer_fallback`

## Numeric gates (additive to v0.1f gates)

| Metric | Gate |
|---|---|
| `background_reasoner_budget_exhaustion_rate` | < 5% (tolerable) |
| `background_reasoner_summary_set_context_attribution_rate` | == 1.0 (every summary reaches foreground) |
| `signal_producer_fallback_rate_reasoner` | < 1% (MCP server should be reliable) |

## Risks

1. **MCP ecosystem maturity**: MCP is newish (2024). Server quality varies. Mitigation: lean on the LLM fallback path; gate MCP rollout on per-server reliability metrics.
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
