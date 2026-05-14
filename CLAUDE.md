# CLAUDE.md — guidance for Claude Code sessions in this repo

## What this project is

A flight-recorder harness for a realtime multimodal companion agent. The architecture spec is frozen at v0.1 in [`docs/architecture-v0.1.md`](docs/architecture-v0.1.md). The current milestone is **v0.1a** (see [`ROADMAP.md`](ROADMAP.md) for adapter scope, contract tests, and numeric gates). The pinned success criterion for v0.1a is:

> v0.1a succeeds when the system can explain every utterance, replay every policy decision, stop when interrupted, wait through thinking pauses, and answer direct questions promptly.

Read the spec before writing code. Notes referenced below cite section numbers in `architecture-v0.1.md`.

## Core invariants (from spec Part 2) — non-negotiable

These are the contract of every implementation. A change that breaks any of them is a regression, regardless of how it's framed.

1. **No unlogged behavior.** Every input event, signal, decision, and action is timestamped, source-attributed, and recorded with its causal predecessors.
2. **No direct Thinker speech.** The proposal-generation process emits structured candidates; the policy layer decides whether any candidate becomes speech.
3. **No memory without provenance.** Every memory item carries `source_event_id`, `created_at`, `confidence`, `salience`, `valid_from/valid_to`, `superseded_by`, and a `user_visible_summary`.
4. **No proactive speech without policy approval.** The foreground model does not unilaterally decide when to speak.
5. **Policy-layer replay must be deterministic.** Given the same recorded signals, the policy layer produces bit-identical decisions. Non-determinism here is a bug.
6. **End-to-end replay is behaviorally tolerance-based.** Live ASR, model decoding, and network jitter vary. Agreement uses the behavioral tuple — `same_action_class` + `same_timing_bucket (+/-200ms)` + `same_interaction_intent` + `same_safety_class`. Text similarity is logged as advisory only, not a pass gate.
7. **User commands are first-class.** `forget that` / `what do you remember about me?` / `why did you say that?` / `less proactive` / `quiet mode` are product features, not afterthoughts.
8. **Silence wins ties.** Proactive speech must earn its right to interrupt the world.
9. **No invented tool progress.** Foreground narration about tool progress requires a corresponding `ToolProgressEvent`. Invented progress in voice is more manipulative than in text.
10. **EventLogger is async and non-blocking on the realtime path.** A logger that adds 150 ms to every interaction sabotages its own latency metrics. If backpressure occurs, emit a `log_drop_or_degrade` event — never silently lose events. The realtime path must never wait on log durability.

## Coding discipline

These four rules are derived from the karpathy-coding-rules skill and adapted to this project. They are not stylistic preferences; they exist because hypothesis revision is only useful if the diff between failing and passing test is small enough to reason about.

### 1. Surface assumptions before coding

State what you're assuming. If uncertain, ask. If multiple interpretations exist, present them — don't pick silently. If something in the spec is unclear, stop, name what's confusing, and read it again before writing.

### 2. Write the minimum code that makes the next contract test pass

Minimum code that solves the problem. Nothing speculative.
- No features beyond what the current ROADMAP task asks for.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify. If 200 lines could be 50, rewrite.

### 3. Make surgical edits; do not refactor opportunistically

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.
- Every changed line should trace directly to the current task.

### 4. Every PR has a verifiable success criterion

Each PR ships exactly one of:
- (a) a stub for a future capability,
- (b) an implementation that turns a `pytest.skip` into a passing test,
- (c) a documentation update.

No mixing. "Make it work" is not a success criterion. "`pytest -k thinking_pause` passes" is.

### Read before write

Use `Read` on every file you intend to edit. Greps and listings are not substitutes — you need to see the structure to make a surgical edit.

## Project-specific rules

These translate the invariants into commits-and-code-review form.

- **Adapter-first.** Any new model or library goes behind an interface in `companion_harness/`. `speak_policy.py` and the test files MUST NOT import a model SDK directly. The adapter is the seam where ablation happens; if it's not there, ablation isn't either.
- **Every `SpeakDecision` carries a `primary_reason_code` from the `ReasonCode` enum.** No free-text reasoning on the policy path. Free-text explanation goes in `redacted_explanation`, which is retention-governed and not used for Tier B replay.
- **Every `Event` carries `caused_by[]`.** Orphan events fail Stage 0 contract tests. The DAG must close.
- **`EventLogger` is async and non-blocking.** The realtime path never waits on log durability. Backpressure emits `log_drop_or_degrade`; it does not block.
- **Free-text fields go through `SensitiveField`.** Never store raw strings in `companion_state` or other audited stores.
- **One PR, one outcome.** See coding rule 4 above.

## Development workflow

- **Local:** editing, git, doc work, lightweight CI (unit tests + Stage 0 replay tests run locally).
- **Remote:** `ssh b200` for model weights, GPU inference, integration tests. Code is git-synced from local to remote; model artifacts stay on b200.
- **Reference:** [`docs/remote-dev.md`](docs/remote-dev.md) for the workflow specifics. Verify b200 and the venv on first remote session — they are not validated at bootstrap.

## What to work on next

See [`ROADMAP.md`](ROADMAP.md) §NEXT TASKS. Pick the lowest-numbered task that isn't done. Do that one. Stop. Ship a PR with the success criterion in the description.

## What this project is NOT

- Not a chatbot.
- Not an LLM wrapper.
- Not a benchmark suite.
- Not a personality experiment.

It is a flight-recorder harness. The first success is a boring replay report: every utterance traceable, zero orphan actions, deterministic policy layer. The texture (Stage 6) does not start until that foundation works.
