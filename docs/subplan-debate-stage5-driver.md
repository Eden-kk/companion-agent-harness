# Subplan — Stage 5: CLI driver + real two-instance debate run + smoke checker

Parent: [plan-two-minicpm-debate.md](plan-two-minicpm-debate.md). Depends on Stages 0–4 complete.

## Scope

Build `scripts/run_two_minicpm_debate.py` (the CLI), wire the moderator seed, the self-voice pre-check, the optional `listen_prob_scale=0.7` retry, and the smoke-checker that validates the three success-criterion invariants on a real run. This subplan does NOT add new orchestrator behavior; it just composes Stages 0–4.

Out-of-scope: per-debater fine-tuning, content judge LLM, replay tool, multi-motion sweeps.

## Inputs (assumed from Stages 0–4)

- Stage 0 already wrote `scripts/probe_minicpm_dual_duplex.py` and recorded its verdict in the plan's revision log (`single` or `dual`).
- Stage 1: `MiniCPMDuplexSession`.
- Stage 2 + 3: `DebateOrchestrator` with full `DebateMetrics`.
- Stage 4: `write_transcript`, `render_audio_from_transcript`, `_resample_24k_to_16k`.
- A `KokoroTtsAdapter` (or equivalent) already initialised — used for moderator-seed render too.
- **Stage 0.c addendum:** Stage 0.c writes the confirmed `ref_audio` kwarg name as `REF_AUDIO_KWARG: str = '...'` constant inside `companion_harness/debate/minicpm_duplex_session.py` so the CLI script does not need to re-read modeling_minicpmo.py.

## CLI surface

```
python scripts/run_two_minicpm_debate.py \
  --motion "This house believes social media has done more harm than good to society." \
  --side-a-stance "Proposition (harm: polarization, addiction, teen harm, eroded shared truth)" \
  --side-b-stance "Opposition (net positive: democratized voice, connection, info access, mobilization)" \
  --max-ticks 30 \
  --k-grace 1 \
  --n-deadlock 4 \
  --t-max 30 \
  --load-mode {single|dual}  \    # default from Stage 0 verdict
  --listen-prob-scale-a 0.9 \
  --listen-prob-scale-b 0.9 \
  --ref-voice-a af_heart \
  --ref-voice-b am_michael \
  --out-dir artifacts/debate/<auto: timestamp>
```

Auto-defaults: if `--out-dir` is not provided, use `artifacts/debate/<UTC YYYYMMDDTHHMMSS>/`. **Stage 0 writes its verdict to `artifacts/stage0_verdict.json`** with schema `{"load_mode": "single"|"dual", "plan_b_engaged": bool, "ref_audio_path": "prepare_kwarg"|"setattr"}` (note: `ref_audio_path` is read by Stage 1 only when constructing `MiniCPMDuplexSession`; Stage 5 reads only `load_mode` and `plan_b_engaged`). The CLI reads that file when `--load-mode` is omitted. If the file is missing, the script exits with a clear error: 'Stage 0 has not been run; pass --load-mode explicitly or run scripts/probe_minicpm_dual_duplex.py first.'

## Execution sequence (the script body)

```
1. Parse args; create out_dir; configure logging to out_dir/run.log. Step 1 is also responsible for logging to run.log: load_mode (resolved from CLI or stage0_verdict.json), plan_b_engaged flag, and the resolved out_dir path. These three values are written to run.log BEFORE any other step runs, so they exist even if step 6 (pre-check) triggers exit code 2.
2. Load Kokoro pipeline once (used for moderator seed + final artifact render).
3. Load MiniCPM base model(s) per --load-mode:
   - single: one AutoModel.from_pretrained, call as_duplex() twice through C1.
   - dual:   two AutoModel.from_pretrained, one as_duplex() per.
4. Render moderator seed audio:
   - Use Kokoro to render the opening line: "Welcome. Tonight's motion: '<motion>'.
     Proposition, the floor is yours — your opening."
   - Resample 24 kHz → 16 kHz via _resample_24k_to_16k. Pad/truncate to CHUNK_SAMPLES.
   - Same for the nudge line: "Let's hear from the floor. Proposition, your move."
5. Build the two MiniCPMDuplexSession instances:
   - System prompts assembled from prompts.py (template + motion + side + stance + name + opp_name).
   - prepare(prefix_system_prompt=...) with ref_audio per Stage 0.c decision.
   - force_listen_count=0.
6. (PRE-CHECK) Self-voice in-distribution check: **Skip step 6 entirely if Plan B is engaged** — under Plan B, audio cross-feed is silence, so the voice-distribution-shift heuristic is undefined. The Plan B run proceeds directly from step 5 to step 7.
   - Run 3 manual ticks: A speaks scripted opener; B receives A's audio_waveform as inbox.
   - For each of B's 3 generate() responses:
       if B.is_listen == False AND (len(B.text) == 0
                                    or (sum(1 for c in B.text if ord(c) > 127)
                                        > 0.5 * len(B.text))):
           save 3 chunks + 3 text outputs to out_dir/diagnostic/
           print "voice-distribution-shift detected" + diagnostic path
           sys.exit(2)
   - If clean, RESET both sessions (re-prepare with original system prompts) before
     the real debate, since the pre-check perturbed their KV cache.
7. Build DebateOrchestrator and call run().
8. Write transcript: debate_artifacts.write_transcript(trace, out_dir/transcript.json).
9. Render artifact audio: debate_artifacts.render_audio_from_transcript(
       trace, kokoro_pipeline=kokoro,
       voice_for={"A": args.ref_voice_a, "B": args.ref_voice_b},
       out_wav=out_dir/debate.wav)
10. Smoke checker (function _smoke_check(trace)):
    - assert trace.metrics.total_ticks > 0
    - assert trace.metrics.max_collision_duration_ticks <= trace.k_grace + 1  # k_grace is a top-level field on DebateTrace, NOT trace.metrics.k_grace
    - if trace.metrics.barge_in_successes >= 1: PASS.
      else:
        - if first run: log warning; **call session.reset(prefix_system_prompt=...) on BOTH sessions before retry** (uses the confirmed reset pattern in finding 1 above); rebuild the DebateOrchestrator with **the same constructor args as the first run except `listen_prob_scale={'A': 0.7, 'B': 0.7}`** (and the same `plan_b_text_supplier` if Plan B is engaged); sessions are reused; orchestrator is fresh; rerun from step 7 (not step 5).
        - if second run still 0: assert False with diagnostic.
11. Print final summary (paths to transcript + WAV, key metrics, mode).
```

## Notes on session reset between pre-check and real run

**Session reset pattern (confirmed from foreground_model_minicpm.py:302 and probe_minicpm_partial_chunk.py:190-191).** MiniCPM-o does NOT support mid-session re-prepare. To reset a duplex session, call `session._duplex.model.reset_session(reset_token2wav_cache=False)` followed by `session._duplex.prepare(prefix_system_prompt=...)`. Expose this as a `MiniCPMDuplexSession.reset(prefix_system_prompt: str)` method (Stage 1 addendum if not already exposed; if not, the script calls the underlying duplex object directly). All session-reset points in this script (post-pre-check, post-first-run-retry) use this pattern.

The script records the reset in `run.log`.

**Stage 1 addendum risk:** Stage 1 must expose `reset(prefix_system_prompt)`; if not, this subplan adds it as a single-method extension on `MiniCPMDuplexSession`.

## Plan B activation path (only if Stage 0.b flagged it)

If Stage 0 set the global flag "Plan B engaged", the script:
- Loads models with `as_duplex(generate_audio=False)`.
- Cross-feeds silence both ways (the orchestrator handles this — no script change).
- Maintains a per-session `last_opponent_text` buffer (init "").
- At every tick BEFORE the orchestrator's prefill, calls `session.prefill(silence, text_list=[last_opponent_text])` indirectly via the orchestrator's Plan B hook (see below).

**Plan B requires an orchestrator-level hook that Stage 2 must add to C2 ahead of time.** Specifically, Stage 2's `DebateOrchestrator.__init__` is extended with `plan_b_text_supplier: Callable[[dict[str, DuplexTickResult], str], str | None] | None = None` (kw-only, default None). When non-None, the orchestrator calls it just before phase-1 prefill for each session: `text_seed = plan_b_text_supplier(last_results, name)`, then `session.prefill(audio, text_list=[text_seed] if text_seed else None)`. With the default None, behavior is unchanged. This is a one-line additive change to C2 — Stage 5 wires the callable, Stage 2 implements the hook. **Action: parent plan's C2 contract must be amended to include this kwarg before Stage 2 begins, OR Stage 2 must be re-run after Stage 5 patches the kwarg in (with all four Stage 2 tests passing again).**
- Smoke checker logic unchanged.

If Plan B is NOT engaged, the orchestrator runs unmodified.

## Verification

End-to-end on b200:

```bash
cd /home/yid042/projects/companion-agent-harness/.claude/worktrees/feat+two-minicpm-debate
/raid/yid042/venvs/companion-harness/bin/python scripts/run_two_minicpm_debate.py \
  --motion "This house believes social media has done more harm than good to society." \
  --side-a-stance "Proposition (harm: polarization, addiction, teen harm, eroded shared truth)" \
  --side-b-stance "Opposition (net positive: democratized voice, connection, info access, mobilization)" \
  --max-ticks 30
```

Expected files after success:
- `artifacts/debate/<ts>/transcript.json` (valid JSON, ≥30 tick records)
- `artifacts/debate/<ts>/debate.wav` (24 kHz mono int16 WAV, loads via `wave.open()`, duration > 1 s)
- `artifacts/debate/<ts>/run.log` (logs load-mode, Plan B engaged flag, smoke-check verdict, all metrics)

Exit code 0 on smoke-check pass; 2 on voice-distribution-shift; 1 on smoke-check fail after retry.

## Risks

- **Pre-check false positive.** B's text contains legitimate non-ASCII (emoji, smart quotes). Mitigation: the >50% threshold is high; if it triggers spuriously, lower to a tighter heuristic (e.g., require both is_listen=False AND empty text AND len > 0). Treat the threshold as a tunable constant, not a hardcoded magic.
- **Session reset failure.** If `prepare()` is single-use, Option B (rebuild) doubles wall-clock for the warmup. Accept the cost.
- **Listen_prob_scale=0.7 still doesn't barge.** Hard-fail with a diagnostic. Operator decides whether to lower further or accept "no natural barge-in" as the experimental finding — that itself is a research result.
- **Kokoro voice not available locally.** Pre-flight: script checks `kokoro.voices` contains both requested voice IDs at startup, exits with a clear message if not.

## Open questions (non-blocking)

- (none — debug audio capture is a future flag, not in scope here).
- Whether the smoke checker should compare the final transcript text against a content judge. Out of scope for Stage 5; follow-up.
