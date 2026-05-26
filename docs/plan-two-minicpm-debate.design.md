# Two-MiniCPM-o debate harness

**Status:** Plan ready (reviewed 4 rounds, 2026-05-26) · **Plan:** [plan-two-minicpm-debate.md](./plan-two-minicpm-debate.md)

## TL;DR

Two MiniCPM-o-4.5 instances debate a fixed motion on a lockstep 1 Hz clock, using the model's native duplex primitives (`break_event`, `listen_prob_scale`, `force_listen_count`) to enforce one-speaker-at-a-time with barge-in. The harness is a standalone research instrument inside `companion_harness/debate/` — it doesn't touch any v0.1a assistant code paths. The end-of-debate artifacts are a per-tick text trajectory (JSON) and a single offline-rendered Kokoro WAV stitched from that trajectory.

## Why

- The TACT-Bench / multi-stream-simulation thread needs a live-fire test of context-driven barge-in on a TDM omni model, and nothing in the repo today exercises two duplex instances against each other.
- Codex review of the design surfaced two load-bearing duplex bugs (break-event lifetime, audio-floor-gating) that we want pinned in code before they leak into the assistant's interruption layer.
- Outcome: a reproducible run that produces one debate transcript + one stitched audio file, with metrics that confirm break-and-yield works on this model.

## Goals

- Two MiniCPM-o instances complete a 30-tick debate with at most one audible speaker per tick (modulo one collision tick before break fires).
- At least one observed barge-in: challenger transitions listen→speak while incumbent was speaking, incumbent force-listened on the following tick.
- Per-tick text trajectory saved as `artifacts/debate/<run_id>/transcript.json`.
- Final stitched Kokoro audio saved as `artifacts/debate/<run_id>/debate.wav` (24 kHz mono).
- Six pinned unit tests (break lifecycle, acoustic overlap, self-yield, livelock, deadlock, T_MAX) run on fakes in the canonical venv.

## Non-goals

- No content judge LLM, no realtime mic/speaker, no vision, no web UI.
- No integration with `realtime_loop.py` / `continuous_orchestrator.py` / `speak_policy.py`.
- No per-debater tuning of `listen_prob_scale` (both start at 0.9, fall back once to 0.7 if no barge-in observed).
- No PR to `main` from this work — lands on a worktree branch; user decides whether to PR.

## Approach

The orchestrator runs the design's lockstep loop verbatim: for each tick, both sessions ingest the opponent's last 1 s of audio via `streaming_prefill`, then call `streaming_generate(listen_prob_scale=...)`. The arbiter keeps three states strictly separate — `audible` (drives metrics), `floor` (policy only, never gates audio), and `break_armed` (who's force-listened next tick). Cross-feed is unconditional in both directions, otherwise the speaker can't perceive a barge-in.

Two duplex sessions live behind a thin `MiniCPMDuplexSession` wrapper that exposes only `prefill / generate / set_break / clear_break / is_break_set`, satisfying the CLAUDE.md adapter-first rule. The orchestrator never imports the MiniCPM SDK; it only sees this wrapper.

Stage 0 is a one-shot probe that resolves three open questions before any orchestrator code runs: (a) whether one loaded base model can host two `as_duplex()` sessions or whether we need two `from_pretrained` loads, (b) whether `as_duplex(generate_audio=True)` constructs on this host (the existing assistant uses `=False` because of a `libcudart.so.13` dependency, and Plan B engages if it crashes), (c) the exact `ref_audio` plumbing path on `MiniCPMODuplex.prepare()`. Stages 1–4 implement the wrapper, orchestrator, and artifact helpers on fakes; Stage 5 is the real two-instance run on b200.

Plan B is a binding fallback contract: if `generate_audio=True` is unavailable, sessions run with `generate_audio=False`, audio cross-feed is silence, and the opponent's last speak text is injected via the confirmed `streaming_prefill(text_list=[...])` side channel. All orchestrator invariants (break lifecycle, K_GRACE, T_MAX, deadlock nudge) are unchanged.

## Key decisions

- Two `MiniCPMDuplexSession` instances, not session multiplexing inside one model (Stage 0 confirms; VRAM headroom on b200 makes 2×9 GB cheap).
- `generate_audio=True` is the primary path; Plan B (silence + text injection) is the fallback (avoids forking the orchestrator into "real audio" and "text-only" variants).
- Floor never gates audio; audio is always cross-fed (fixes the Codex-surfaced barge-in-perception bug).
- `break_event` is armed in tick t's arbitrate step and cleared only after tick t+1's generate fires it (fixes the original break-cleared-before-it-fires bug).
- Same-tick floor handover on collision-resolution: `arm_break(incumbent)` AND `floor = challenger` happen together, matching the design's pseudocode line 145.
- Artifact audio is Kokoro-rendered offline from the text trajectory, NOT the per-chunk MiniCPM audio — keeps the user-facing artifact clean and decoupled from MiniCPM's TTS quality.

## Risks & mitigations

- `generate_audio=True` ImportError on libcudart.so.13 → Stage 0.b fast-fails to Plan B (silence + `streaming_prefill(text_list=[...])`); orchestrator interfaces unchanged.
- Self-voice cross-distribution shift (B can't make sense of A's TTS) → Stage 5 runs a 3-tick pre-check; on diagnostic-dump trigger, operator decides retry-with-different-voices or engage Plan B.
- Models too polite → barge-in count = 0 → Stage 5 smoke checker reruns once at `listen_prob_scale=0.7`; second zero is hard fail.
- `prepare(ref_audio=...)` kwarg name uncertain → Stage 0.c reads `modeling_minicpmo.py`; only the wrapper's internal call shape changes, interface stable.

## Rollout

Single worktree branch (`feat/two-minicpm-debate`). No feature flag — the new `debate/` package is opt-in by import; nothing else in the repo references it. Verification gate before completion: Stage 5 driver produces both artifacts, the smoke checker passes the three success-criterion assertions (`barge_in_successes >= 1`, `max_collision_duration_ticks <= k_grace + 1`, `total_ticks > 0`), and all six fake-driven unit tests pass in the canonical venv (`/raid/yid042/venvs/companion-harness/bin/python -m pytest tests/test_debate_*.py`).

## Open questions

- Whether `MiniCPMODuplex.prepare()` takes `ref_audio` as a kwarg or it's set on the duplex object beforehand. (resolved by Stage 0.c read of `modeling_minicpmo.py`)
- Whether a single loaded base model permits two independent `as_duplex()` sessions or each session needs its own `from_pretrained` (resolved by Stage 0 probe; affects VRAM only, not interfaces).
- Whether the moderator seed should be a Kokoro render or pre-recorded silence + a one-line text injection. (deferred to Stage 5 — default is Kokoro render at 24 kHz, resampled to 16 kHz for MiniCPM ingest.)

---
*This doc is generated from the implementation plan and reflects the plan as of convergence. The plan is the source of truth for execution; this doc is for readers who need the gist.*

_Last regenerated: 2026-05-26_
