# Design: Parallel proposers for sync talk-while-think

**Status:** DRAFT — for review (round 0)
**Scope:** model-graph design; how `ForegroundModel`, `ThinkerProposalGen`, and `BackgroundReasoner` are operated to deliver a "talk-while-thinking" texture without retraining the foreground model.
**Out-of-scope:** new model training; native multi-stream architectures; foreground KV-cache surgery; any new SpeakPolicy decision categories.

---

## 1. Context

The frozen spec ([`docs/architecture-v0.1.md`](architecture-v0.1.md)) Part 3 already declares three proposer-side adapter slots:

- `ForegroundModel` — full-duplex multimodal speech/text (currently `MiniCPM-o`).
- `ThinkerProposalGen` — proposal-only, no direct speech path.
- `BackgroundReasoner` — async tool/reasoning model.

Part 5 defines a `ThinkerProposal` schema. Part 6 Stage 6 requires that "Thinker emits proposals only. Cannot speak directly. All proposals pass through Stage 3" (SpeakPolicy).

What the spec does *not* fix:
- The **clock** each proposer runs on (per-turn vs. continuous).
- The **selection logic** SpeakPolicy uses when multiple proposers have active candidates.
- Whether `ForegroundModel`'s per-turn output and `ThinkerProposalGen`'s standalone output flow through the same `ThinkerProposal` event type.

Today's implementation runs `ForegroundModel` per-turn on EOU and treats `ThinkerProposalGen` and `BackgroundReasoner` as either invoked-on-demand or scaffold-only. The result is a **single-stream pipeline** where the model spends its time either *thinking* or *speaking*, never both. The user-perceived experience is "speak → wait → reply"; the substrate cannot produce the "the assistant came in already prepared" texture that the v0.1 spec aspires to (Part 1: "perceptual companionship... notices what you don't, occasionally remarks on it").

## 2. Motivating reference

The 2605.12460 multi-stream LLM line of work (Su, Yang, Li, Geiping; verify against the actual PDF before final citation), together with Moshi and SyncLLM, demonstrates that *one* model can produce concurrent thought + speech streams through architectural parallelism (depth transformer, parallel decoding heads). The harness explicitly declines to reproduce this at the model level (architecture-v0.1.md Part 10). The question this design answers is: **what is the cheapest harness-level equivalent that respects the existing invariants and adapter shape?**

## 3. Goals and non-goals

**Goals:**
- At policy-decision time, SpeakPolicy can choose from proposals generated *before* the current EOU — i.e., generated while the user was still speaking or while the previous utterance was rendering.
- Foreground inference latency unchanged.
- All ten invariants preserved unmodified.
- Tier-B replay determinism unmodified.
- Three small commits, each independently shippable behind opt-in flags.

**Non-goals:**
- No new model training.
- No Reflex / filler-emitter model (rejected; §5).
- No mid-generation foreground context injection (rejected; §6).
- No change to the `SpeakDecision.action_type` enum.

## 4. Design

### 4.1 Stream topology

Three proposer streams, joined at `SpeakPolicy.decide()`:

```
ForegroundModel        ─── proposals ───┐
(MiniCPM-o, per-turn on EOU)            │
                                        ▼
ThinkerProposalGen     ─── proposals ───┤
(continuous, ~2 Hz tick)                │── SpeakPolicy.decide()
                                        │   selects among active
BackgroundReasoner     ─── proposals ───┤   ThinkerProposal events
(continuous, ~0.5 Hz tick;              │
 plus solicited tool work as today)     │
                                        │
SleepTimeAgent ──── writes companion_state ──┘
(idle, async; unchanged)         (consumed as PolicyInputs)
```

Each `ThinkerProposal` event carries the spec-defined fields (Part 5) plus:
- `source_adapter: str` — provenance for replay/audit.
- `ttl_ms: int` — proposal expiry; SpeakPolicy ignores expired candidates.

Foreground decoding is **unmodified**. The orchestrator's existing per-turn flow (EOU → ASR → addressing → memory.retrieve → SpeakPolicy → ForegroundModel → TTS) stays intact. The additions are two continuously-running proposer adapters whose outputs become inputs to `SpeakPolicy`.

### 4.2 SpeakPolicy selection logic

At decision time, SpeakPolicy:

1. **Gather** — collect `ThinkerProposal` events where `created_at + ttl_ms > now`. Add the just-emitted foreground proposal (per-turn, if any) as a candidate.
2. **Mode-gate** — drop candidates blocked by current `privacy_mode`, `social_mode`, `risk_mode`, cooldowns, budgets (existing Stage 3 logic).
3. **Rubric-gate** — for `aesthetic_reaction` candidates, apply the 8-check rubric (existing Stage 6 logic).
4. **Rank** — surviving candidates ranked by `(novelty × confidence) / interruption_cost` (OQ-4). Default to `silence` if no candidate exceeds the per-mode threshold.

Determinism property: given a recorded sequence of `(PolicyInputs, ThinkerProposal events)`, selection is bit-identical. Replay does not re-run proposer adapters — it consumes recorded proposal events. This is the standard event-sourcing pattern.

### 4.3 Continuous-tick loop for ThinkerProposalGen

```python
class ContinuousThinkerProposalGen:
    tick_interval_ms: int = 500          # OQ-1
    proposal_ttl_ms: int = 5_000         # OQ-2

    async def run(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.tick_interval_ms / 1000)
            recent = self._event_log.recent(window_ms=10_000)
            if not self._should_propose(recent):
                continue                 # the common case; silence wins ties
            proposal = await self._generate(recent)
            if proposal is None:
                continue
            self._event_log.log(ThinkerProposal(
                ...,
                source_adapter="thinker_proposal_gen_continuous",
                ttl_ms=self.proposal_ttl_ms,
                caused_by=[e.event_id for e in self._evidence_for(proposal)],
            ))
```

Key properties:
- Runs as a background `asyncio` task; never blocks foreground.
- `_should_propose` is a cheap gate (no model call). Default policy: return `False` unless current `social_mode == background_presence` AND at least one of {scene_change, deictic_reference, recent_user_utterance_with_novelty} fired in the window. Preserves invariant #8 by making "no proposal" the default.
- `_generate` does the model call only when `_should_propose` permits.

### 4.4 BackgroundReasoner unsolicited-proposal mode

Today: `BackgroundReasoner` is invoked on-demand for tool-routed reasoning. Returns results that feed the next foreground turn.

Change: add a `run_continuous(tick_interval_ms=2_000)` mode. At each tick, consider whether the recent event-log state warrants an unsolicited proposal (e.g., "user mentioned X three turns ago; here's a follow-up question worth offering"). If yes, emit `ThinkerProposal` of `proposal_type ∈ {observation, question, memory_bridge}`. Solicited tool work unchanged.

The continuous mode is opt-in via `--enable-background-reasoner-continuous`, default off.

## 5. Why not Reflex (rejected)

An earlier sketch added a fourth adapter: a ~100M LM emitting stall tokens at the speech-slot rate so the user is never in silence ("Mm, let me check...", "Okay so..."). **Rejected.** Reasons:

| Concern | Source |
|---|---|
| Conflicts with invariant #8 "Silence wins ties. Proactive speech must earn its right to interrupt the world." | `architecture-v0.1.md` Part 2 |
| Conflicts with Part 1 texture target ("not a chatbot that fills silence... restraint earned through restraint") | Part 1 |
| Conflicts with Stage 6 proactivity budgets (`aesthetic_reaction_budget: 1 per 60 min`, `creative_focus: disabled`, `sleep_winddown: disabled`) | Part 6 §Stage 6 |
| Legitimate filler cases already covered by `ToolProgressEmitter` with invariant #9 evidence-binding | `model-stack.md` N3 |

A model whose purpose is filling latency gaps is structurally the over-talkative failure mode Stage 3 is built to prevent. The sync-talk-think texture is not "the assistant fills silence" — it is "the assistant comes in already prepared because parallel proposers thought ahead." Reflex confuses these.

## 6. Why not mid-generation foreground context injection (rejected)

An earlier sketch had `BackgroundReasoner` write a scratchpad that `ForegroundModel` reads between decoding steps (KV-cache surgery: pause decode, append context, re-prefill, resume). **Rejected.** Reasons:

- **Per-injection latency:** re-prefill of delta tokens + KV-cache invalidation past the injection point + re-rope. Realistically 30–150 ms on a 7B-class model for a 50–200 token delta.
- **Compounding cost:** if Deliberation writes at 1 Hz and the foreground utterance lasts 2 s, per-token latency degrades through the back half of the utterance.
- **Engineering risk:** mid-stream context shifts violate the model's autoregressive training distribution; outputs become stitched. MiniCPM-o is not trained to handle this gracefully.
- **Determinism risk:** wall-clock injection timing is non-deterministic; bit-identical Tier-B replay would require recording the injection sequence as events and replaying them exactly, doubling event-log surface and creating a new replay tier.

The sync-talk-think property does not require this. It is delivered across turns by §4.1's stream topology, and at decision time by §4.2's selection logic.

## 7. Invariant compliance

| Invariant | Compliance argument |
|---|---|
| #1 No unlogged behavior | Every proposal is a logged `ThinkerProposal` event with `caused_by[]`. Continuous-tick adapters log their decision *not* to propose only when an actual gate fired (avoid log spam). |
| #2 No direct Thinker speech | `ThinkerProposalGen` / `BackgroundReasoner` emit proposals only. `SpeakPolicy.decide()` is the only path to speech. Unchanged from spec. |
| #3 No memory without provenance | Proposers may read memory; they don't write. `SleepTimeAgent` remains the only memory writer. |
| #4 No proactive speech without policy approval | Every proposal is gated by `SpeakPolicy.decide()`. Continuous emission does not bypass this. |
| #5 Policy-layer replay deterministic | Given recorded `(PolicyInputs, ThinkerProposal events)` sequence, selection is bit-identical. Wall-clock proposer concurrency is irrelevant to replay. |
| #6 Tolerance-based E2E replay | Unchanged. |
| #7 User commands first-class | Unchanged. `less proactive` and `quiet mode` are honored by mode-gating in §4.2 step 2; continuous proposers do not override them. |
| #8 Silence wins ties | `_should_propose` defaults to `False`. SpeakPolicy still defaults to silence when no candidate exceeds the per-mode threshold. More proposals available != more speech. |
| #9 No invented tool progress | `BackgroundReasoner` unsolicited proposals about tool state must cite `ToolProgressEvent`s via `caused_by[]`. A new contract test enforces this. |
| #10 EventLogger async non-blocking | Proposers write through EventLogger; backpressure handled identically. |

## 8. Latency budget

| Component | Cost added to foreground critical path |
|---|---|
| Existing foreground pipeline | unchanged |
| `ContinuousThinkerProposalGen.run()` on separate asyncio task | 0 ms |
| `BackgroundReasoner.run_continuous()` on separate asyncio task | 0 ms |
| SpeakPolicy reads proposal events at decision time | <1 ms (bounded-window list comprehension) |
| Tier-B replay | unaffected — wall-clock concurrency irrelevant |

**Out-of-budget costs (acceptable):**
- **GPU memory** — two additional continuously-loaded models. On b200 (80 GB): MiniCPM-o ~24 GB; planned BackgroundReasoner (Nemotron-3 Nano Omni) ~16 GB; a 1–3B continuous ThinkerProposalGen adds ~6–12 GB. Headroom remains for vision side-channel and TTS. Confirm empirically before flipping default-on.
- **GPU compute** — continuous-tick model calls add an estimated 10–20% steady-state GPU utilization above today's bursty per-turn pattern. Reduces with `_should_propose` gate tuning (OQ-1).

## 9. Implementation sketch

Three commits, each shippable independently. All opt-in flags default off, matching the v0.2e rollout pattern.

**Commit 1 — `ThinkerProposal` event-source plumbing**
- `companion_harness/thinker_proposal.py`: bring the spec's Part 5 `ThinkerProposal` schema into code as a dataclass; add `source_adapter` and `ttl_ms` fields.
- `companion_harness/speak_policy.py`: extend `PolicyInputs` to include `active_proposals: tuple[ThinkerProposal, ...]`; extend `decide()` per §4.2.
- Bump `POLICY_VERSION = "v0.1k"` (per the project's monotonic-version discipline; the policy path now consumes proposal events as input).
- Contract tests:
  - `test_speak_policy_selects_from_active_proposals` — given a fixture of N proposals + PolicyInputs, assert deterministic selection.
  - `test_speak_policy_respects_proposal_ttl` — expired proposals are ignored.
  - `test_speak_policy_replay_bit_identical_with_proposals` — Tier-B replay determinism.

**Commit 2 — Continuous ThinkerProposalGen adapter**
- `companion_harness/thinker_proposal_gen_continuous.py`: implement `ContinuousThinkerProposalGen` per §4.3.
- Wire into `realtime_orchestrator.py` as opt-in `--enable-thinker-continuous` (default off).
- Contract tests:
  - `test_thinker_proposal_gen_emits_within_tick` — start the loop under `SyntheticClock`; advance N ticks; assert ≥ 1 proposal event emitted when gate permits.
  - `test_thinker_proposal_gen_silent_when_gate_denies` — `_should_propose` returns `False`; no proposal events.
  - `test_thinker_proposal_does_not_speak_directly` — invariant #2 enforcement.

**Commit 3 — BackgroundReasoner continuous mode**
- `companion_harness/background_reasoner.py`: add `run_continuous()` per §4.4.
- Wire into orchestrator as opt-in `--enable-background-reasoner-continuous` (default off).
- Contract tests:
  - `test_background_reasoner_emits_unsolicited_proposal` — given a fixture event log with deferred follow-up condition, assert appropriate proposal.
  - `test_background_reasoner_tool_progress_evidence_bound` — unsolicited proposals citing tool state must transitively reference a `ToolProgressEvent` in `caused_by[]`.

## 10. Test plan

**Stage 0 / harness-native (must pass before flag flip):**
- All three commits' contract tests above.
- `test_continuous_proposers_do_not_increase_speech_rate` — given a baseline fixture session and a `--enable-thinker-continuous` rerun, assert `(num_full_response_decisions + num_aesthetic_reaction_decisions) ≤ baseline + epsilon`. Operationalizes invariant #8.

**Stage 1 latency regression:**
- `test_foreground_latency_unaffected_by_continuous_proposers` — run `direct_question_latency` fixture with continuous proposers on and off; p50/p95 within tolerance. Already-shipped Stage 1 latency budgets (full_response p50 <800 ms, p95 <1500 ms) must hold.

**Stage 6 texture:**
- `test_aesthetic_reaction_budget_respected_with_continuous_proposers` — continuous proposers do not cause budget overruns.
- `test_creative_focus_mode_blocks_continuous_proposers` — `social_mode = creative_focus` → `_should_propose` returns `False`; no proposals emitted.

## 11. Replay determinism (detail)

The interesting property is that replay does NOT re-run continuous proposers. Tier-B test fixture is:

```
(PolicyInputs sequence, ThinkerProposal event sequence) → expected SpeakDecision sequence
```

The event log totally orders proposal events; replay reads them in original order. Wall-clock concurrency between proposers is therefore irrelevant to Tier-B (invariant #5). This matches the harness's existing event-sourcing pattern and the established Tier-A vs. Tier-B split (Part 6, Stage 0).

Tier-A (live-model) replay does re-run proposers; agreement uses the behavioral tuple (`same_action_class + same_timing_bucket ±200 ms + same_interaction_intent + same_safety_class`). Text similarity remains advisory.

## 12. Open questions

| OQ | Question | Resolution path |
|---|---|---|
| OQ-1 | Tick rate for `ContinuousThinkerProposalGen` (proposed 2 Hz)? | Empirical, measured against Stage 6 `false_proactive_utterances_per_hour` after first integration. |
| OQ-2 | Proposal TTL default (proposed 5 s)? | Empirical; short enough to stay fresh, long enough to survive one user utterance. |
| OQ-3 | `_should_propose` — hand-rule or learned classifier? | Hand-rule for v0 (rate-control, not content selection). Learned variant deferred. |
| OQ-4 | SpeakPolicy ranking formula when multiple candidates pass mode-gates? | Proposed: `(novelty × confidence) / interruption_cost`. Subject to plan-critic review and Stage 6 validation. |
| OQ-5 | Wrap `ForegroundModel`'s per-turn output as a `ThinkerProposal` for uniform handling, or treat it as a special-cased always-eligible candidate? | Architectural call. Uniform handling is cleaner; special-casing is less code change. Recommend uniform after Commit 1 lands. |
| OQ-6 | Should continuous proposers be paused during `privacy_mode = sensitive_conversation` regardless of mode-gate? | Likely yes; privacy mode pauses are spec-level, not per-decision. Add to §4.3 `_should_propose` gate. |

## 13. Rollout plan

1. Land Commits 1–3 with all flags default-off. Target v0.2.x.
2. Add manual-test handbook scenario for `--enable-thinker-continuous`. Run b200 manual test cycle.
3. Tune OQ-1, OQ-2, OQ-4 empirically using `scripts/v0_2_replay_report.py` and 1-week RCT methodology (Stage 6 §Eval).
4. Default-on after one clean manual-test cycle + Stage 6 metric gates green. Earliest v0.3.
5. Continuous-proposer adoption is reversible: flip flags off, behavior reverts to current single-stream pipeline.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Continuous proposers degrade event-log signal-to-noise (too many no-op proposal events) | `_should_propose` gate is the rate-controller; log only on actual proposal emission, not on every tick. Add `thinker_tick_gated_out` counter, not event-per-tick. |
| GPU memory budget breach when ThinkerProposalGen model + BackgroundReasoner + foreground + vision sidecar coexist | Confirm empirically on b200 before flag flip. If breach: distill ThinkerProposalGen smaller or share weights with foreground. |
| Determinism breakage if continuous-tick adapters use wall-clock `time.monotonic_ns()` for tick scheduling | Tick scheduling must use the `SyntheticClock` injected at orchestrator construction (per Eval Phase A.5 pattern). Contract test enforces. |
| Spec drift between this design and `architecture-v0.1.md` Parts 3/5/6 | This design is a *refinement* of existing adapter slots, not a new architecture. No spec amendment required. Confirm during plan-critic review. |

## 15. Out of scope / future

- **Reflex / filler-emitter model** — rejected (§5).
- **Mid-generation foreground context injection** — rejected (§6).
- **Native multi-stream foreground model** — would require retraining; not in v0.2/v0.3 scope. Tracked as a long-horizon possibility if a suitable open model appears.
- **Learned `_should_propose` classifier** — deferred (OQ-3); hand-rule first.
- **Cross-proposer deduplication** (two proposers emit semantically similar candidates simultaneously) — observable problem only after empirical run; defer until measured.

## 16. References

- [`docs/architecture-v0.1.md`](architecture-v0.1.md) — frozen spec; especially Parts 3, 5, 6 (Stage 6), 10.
- [`docs/model-stack.md`](model-stack.md) — current adapter inventory and seam status.
- [`CLAUDE.md`](../CLAUDE.md) — coding discipline; adapter-first invariant.
- arxiv 2605.12460 (Su, Yang, Li, Geiping, "Multi-Stream LLMs: Unblocking Language Models with Parallel Streams of Thoughts, Inputs and Outputs") — motivating reference. **Verify against the actual PDF before final citation; the title and authors came through `WebFetch` and were not yet eyeballed by a human.**
- Moshi (Defossez et al., Kyutai, 2024) — already-cited native-multi-stream reference (Part 10).
- SyncLLM (Veluri et al., 2024) — alternative synchronous-decoding architecture.
- Fowler, *Event Sourcing* — replay/determinism pattern foundation.

---

## Appendix A — Why this is a refinement, not a new architecture

The spec already declares `ThinkerProposalGen` and `BackgroundReasoner` as adapters that emit `ThinkerProposal` events gated by SpeakPolicy. This document does not add new adapter slots, new event types, or new policy actions. It specifies:

- The **clock** on which these adapters run (continuous, not per-turn).
- The **selection logic** SpeakPolicy uses (gather → mode-gate → rubric-gate → rank).
- The **TTL** semantics for stale proposals.

Reviewers can treat this as a "behavior specification for existing spec-declared seams," not a v0.2 spec amendment.

## Appendix B — Naming convention check

This branch is `docs-design-parallel-proposers` (no slash, matching the repo's existing `docs-*` branch convention). Filename `design-parallel-proposers-sync-talk-think.md` matches existing `design-vad-tagged-frame-boundary.md` pattern.
