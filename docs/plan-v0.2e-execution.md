# Plan — v0.2e Execution (Adapter default-on decision wave)

## Status: **DRAFT** — drafted 2026-05-16. Source roadmap: `docs/roadmap-v0.2-draft.md` Wave 5 (Task 16). Not yet plan-critic'd; not yet sub-agent dispatched.

> v0.2e is the **per-adapter default-on review wave** for the opt-in real adapters that landed across v0.1j and the post-Round-4 sweep (PR #255 + #260/#262/#263/#264/#271 + diarization once v0.2b lands). Each adapter is evaluated on a fixed 3-criterion rubric (GPU footprint, p95 latency contribution, false-positive rate). Where all three pass, `--enable-X` is flipped to default-ON in `manual_test_console/server.py` via a **single tiny PR per adapter**. Where any criterion fails, default stays OFF and the failure is recorded in the PR body for re-evaluation in v0.3.
>
> v0.2e ships **no new model code, no new policy code, no new contract surface**. It is a measurement-and-config wave: profiling + decision + one-line argparse default flip per adapter. Each PR is independently revertible (one-line `--no-enable-X` operator override, plus a clean revert SHA).
>
> Pure docs-and-defaults discipline: every flip cites profiling evidence in the PR body. No flip ships without numbers.

---

## §0 Dependency on v0.2a / v0.2b / v0.2c

**v0.2e cannot start until v0.2a, v0.2b, and v0.2c are merged.** Each wave contributes adapters that v0.2e measures:

- **v0.2a (Real BackgroundReasoner — roadmap Wave 1)** — does NOT add a new opt-in adapter under measurement here. Listed for completeness; v0.2e excludes BackgroundReasoner from the per-adapter rubric (its rollout is governed by `background_reasoner_budget_exhaustion_rate < 0.05` per roadmap Anchor on `BACKGROUND_REASONER` flag default, not by the GPU/latency/false-positive rubric in this plan).
- **v0.2b (Real diarization — roadmap Wave 2)** — adds `--enable-diarization` (roadmap Task 11). v0.2e Task T9 evaluates it. If v0.2b has not landed by the time v0.2e Wave-A finishes, T9 ships in v0.3 instead and v0.2e closes at 8 PRs max.
- **v0.2c (Real benchmark data loaders — roadmap Wave 3)** — does NOT add a runtime opt-in adapter. Listed for completeness; out of scope here.

**Why this gate is hard, not soft:** v0.2e's measurement targets must exist in the live pipeline before they can be profiled. Flipping a default for a flag that doesn't yet exist is meaningless. If v0.2b slips, T9 slips with it; the other 8 tasks proceed.

The 8 adapters in scope, sourced from `docs/model-stack.md` § "Real models (operational)" rows 13–18 plus the v0.2b additions:

| # | Adapter | CLI flag | Backend | Model-stack row |
|---|---|---|---|---|
| 1 | `CLIPSceneChangeScorer` | `--enable-clip-scene` | CLIP-base on CPU | row 13 |
| 2 | `HeuristicAVConflictScorer` | `--enable-av-conflict` | OpenCV+VAD CPU | row 14 |
| 3 | `MiniCPMDeicticDetector` | `--enable-deictic` | MiniCPM-o text-only chat | row 15 |
| 4 | `ProsodyLexiconUrgencyScorer` | `--enable-urgency` | lexicon+prosody CPU | row 16 |
| 5 | `GroundingDINOAdapter` | `--enable-grounding` | grounding-dino-tiny GPU | row 17 |
| 6 | `SentenceTransformerEmbedder` | `--enable-embeddings` | all-MiniLM-L6-v2 CPU | row 18 |
| 7 | `AttachmentRiskMonitor (live-wired)` | `--enable-attachment-risk` (TBD per OQ-3) | event-stream heuristic | row 11 |
| 8 | `PyannoteDiarizationAdapter` | `--enable-diarization` (v0.2b) | pyannote 3.1 GPU | new in v0.2b |

(Adapter #7 — AttachmentRiskMonitor — is already wired live per PR #234, but its on/off posture has never been formalized as a rubric pass. OQ-3 below decides whether it gets a flag at all or is treated as "always-on, monitored.")

---

## §Dependency graph

```
┌──────────────────────────────────────────────────────────────────────┐
│ Wave-Pre (gates — must merge before any v0.2e PR opens):            │
│   v0.2a Tasks 1-6 merged                                             │
│   v0.2b Tasks 7-11 merged  (gate for T9 specifically)                │
│   v0.2c Tasks 12-14 merged                                           │
└─────────┬────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Wave-A (per-adapter profiling + decision; max 8-way parallel)       │
│   T1 CLIP scene change                                               │
│   T2 AV conflict                                                     │
│   T3 deictic                                                         │
│   T4 urgency                                                         │
│   T5 grounding DINO                                                  │
│   T6 sentence-transformer embeddings                                 │
│   T7 attachment-risk posture (OQ-3 dependent)                        │
│   T8 diarization (gated on v0.2b merge)                              │
└─────────┬────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Wave-B (closeout):                                                   │
│   T9 cumulative changelog row in manual-test-handbook.md +           │
│       model-stack.md "default-on status" column populated            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## §Concurrency map

| Wave | Concurrency | Tasks | Notes |
|---|---|---|---|
| Pre | sequential gate | v0.2a + v0.2b + v0.2c | Hard gate; v0.2e does not open any PR until these merge. T8 gates on v0.2b specifically; if v0.2b slips, T1–T7 still proceed. |
| A | **8×** (one agent per adapter) | T1, T2, T3, T4, T5, T6, T7, T8 | Each task is one PR. No shared file edits — each PR touches `manual_test_console/server.py` argparse default at a different line, plus the PR body's profiling block. **Anchor: one-at-a-time merge (not one-at-a-time author)** — OQ-2 below resolves that PRs are *authored* in parallel but *merged serially* so any regression bisects cleanly to one adapter flip. |
| B | sequential | T9 | Closeout doc sweep — must come after every Wave-A PR merges (or defers). |

**Max realistic concurrency: 8 agents** (Wave-A fan-out). **Realistic gate: review bandwidth + b200 profiling slot scheduling** (each agent needs ~10 min on b200 for profiling — see §profiling rig).

---

## §Profiling rig (shared substrate; not a code task)

Every Wave-A task uses the same 3-stage profiling recipe. Recording this here so each task references it instead of re-defining it.

**Stage 1 — GPU footprint measurement.**
- Run `manual_test_console/server.py --enable-X` (no other adapters enabled) under canonical venv on b200.
- After 60s of warm-up + a single sample interaction, capture `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` and subtract the baseline (`--enable-X` OFF) reading taken in the same session.
- Record delta in MiB; pass criterion: **< 1024 MiB OR adapter is CPU-only (delta == 0)**.
- For CPU-only adapters (rows 1, 2, 4, 6 above), capture RSS via `ps -o rss= -p <server_pid>` instead and record in MiB; pass criterion is "< 500 MiB additional RSS" for the CPU rubric line.

**Stage 2 — p95 latency contribution measurement.**
- Use the eval Phase A.5 `FixtureScenarioDriver` (`companion_harness/evals/scenarios/`) with the `clean_speech_chunked_001` fixture, replayed twice: once with `--enable-X` OFF, once with `--enable-X` ON, both under canonical venv on b200.
- For each replay, extract the relevant per-call duration from the event log (event name per adapter table below).
- Report `p95(on) - p95(off)` in ms; pass criterion: **< 50 ms**.
- Per-adapter event-name mapping:
  - CLIP scene change → `vision_frame.scene_change_score_ms` (synthetic field; pull from `caused_by[]` durations of `vision_frame` events)
  - AV conflict → `vision_frame.audio_visual_conflict_ms`
  - Deictic → `vision_frame.deictic_reference_ms` (MiniCPM chat call)
  - Urgency → `addressing_classified.urgency_score_ms` (composite scorer)
  - Grounding DINO → `vision_frame.grounding_confidence_ms`
  - Embeddings → `memory_retrieval_event.embedding_ms`
  - Attachment-risk → `policy_decision.attachment_risk_ms` (event-stream heuristic; expected near zero)
  - Diarization → emitted by Wave 2 Task 8 per roadmap; field name TBD when v0.2b lands — task body must cite it
- If the field name is not yet emitted in main, the task PR description records "field-name TBD; using wall-clock around `<exact call site>` instead" and the task owner adds the timing field in the same PR as a one-line emission (this is the **only** out-of-scope code change Wave-A permits; everything else is config-only).

**Stage 3 — false-positive rate measurement.**
- Per-adapter ground-truth fixture from `docs/model-stack.md` test corpus:
  - CLIP scene change → `clip_scene_no_change_001` (a single static scene; expected score < 0.2; FP = score crosses any policy gate threshold)
  - AV conflict → `av_no_conflict_001` (aligned audio+video; FP = `audio_visual_conflict_score > 0.5`)
  - Deictic → `deictic_no_pointing_001` (declarative speech, no deictic; FP = `deictic_reference=True` or `deictic_ambiguous=True`)
  - Urgency → `urgency_calm_speech_001` (non-distress phrases; FP = `urgency_score > 0.5`)
  - Grounding DINO → `grounding_unknown_object_001` (vocab miss; FP = `grounding_confidence > 0.5`)
  - Embeddings → `embeddings_unrelated_query_001` (query unrelated to corpus; FP = top-1 cosine > 0.7 against unrelated memory)
  - Attachment-risk → `attachment_risk_neutral_001` (neutral conversational exchange; FP = `attachment_risk_level > 0.3`)
  - Diarization → `diarization_acoustic_feedback_001` (per roadmap fixture manifest; FP = false-speaker-change rate per roadmap gate `diarization_false_speaker_change_rate < 0.05`)
- Each fixture is replayed under canonical venv on b200; the FP rate is `(FP events) / (total relevant events)`.
- Pass criterion is per-adapter:
  - Vision / urgency / attachment-risk: **< 0.05** (1-in-20 false positives, in line with `dataset_row_skip_rate` precedent)
  - Embeddings: **< 0.10** (retrieval is recall-tolerant; FP cost is "retrieved an irrelevant memory" rather than "triggered a bad action")
  - Diarization: use roadmap gate `diarization_false_speaker_change_rate < 0.05` directly
- Each task PR cites the exact fixture path AND the exact pass threshold it used.

**Fixtures that do not yet exist** are filed as a one-line follow-up issue inside the task PR body. The PR can still flip the default if Stages 1+2 pass AND a "best-effort" FP measurement is provided against the nearest available fixture (with explicit "fixture #XXX needs to exist for a clean re-eval" note). This is the **escape valve** for the wave — without it, fixture authoring becomes a v0.2e blocker, which it is not.

---

## §Foundation-first ordering

**Wave-Pre (v0.2a/b/c) is a blocking gate.** No v0.2e PR opens until those waves merge. The profiling rig (§profiling rig above) is documentation-only — it lives in this plan, not in code, so no PR is needed to establish it. Each Wave-A task PR cites this §profiling rig section by name in its description.

---

## §Anchors (locked)

- **Anchor 1 — One PR per adapter flip.** No batch flips. Each adapter is its own revertible unit. Even if 6 adapters pass in the same week, they ship as 6 PRs (max 8 in this wave).
- **Anchor 2 — One-at-a-time merge.** PRs may be **authored in parallel** (Wave-A fan-out), but they merge **serially** so that any post-flip regression bisects to a single adapter. The author of a flipped PR rebases on `main` after the previous PR merges.
- **Anchor 3 — Profiling evidence in PR body is mandatory.** A PR without numbers in the body is auto-rejected by review. The PR body MUST contain a "## Profiling evidence" section with three sub-sections (GPU/CPU footprint, p95 latency, false-positive rate) and the exact command + output for each.
- **Anchor 4 — Rollback note in PR body is mandatory.** Every PR body MUST contain a "## Rollback" section naming **both** (a) the operator-side one-line `--no-enable-X` flag override that returns to the OFF posture without a revert AND (b) the revert SHA (filled in post-merge by the merger). Without (a) and (b), no flip.
- **Anchor 5 — No new code paths.** v0.2e PRs change at most: `manual_test_console/server.py` argparse defaults, `docs/manual-test-handbook.md` cumulative changelog, `docs/model-stack.md` "default-on status" column. The Stage-2 escape valve (one-line timing-emission patch when an event field is missing) is the **only** out-of-scope code edit allowed, and only if explicitly cited in the PR body's profiling section.
- **Anchor 6 — Tier-B replay safety.** Every PR must include the result of `pytest tests/test_policy_replay_exact.py -q` under canonical venv showing 100% pass before and after the flip. A default flip changes the **default** PolicyInputs distribution but must NOT change a single recorded fixture's replay outcome (invariant #5). If a fixture fails, the flip is rolled back and the fixture is investigated as a regression.

---

## §Open Questions (with leans)

- **OQ-1 — Which adapters are in scope?** **Lean: the 8 above** (6 from PR #255 + AttachmentRiskMonitor + Diarization-once-v0.2b-lands). Excludes BackgroundReasoner (different rollout regime per roadmap Anchor 2) and TTS adapters (native MiniCPM TTS is opt-in per `--tts-adapter`, not per `--enable-X`; its default flip is a separate v0.3 decision tied to libcudart resolution).
- **OQ-2 — Author-parallel or merge-parallel?** **Resolved as Anchor 2: author-parallel, merge-serial.** Rationale: 8-way author parallelism unlocks throughput; merge-serial preserves clean bisect when a flipped default later turns up a regression in a multi-day manual test.
- **OQ-3 — Does AttachmentRiskMonitor get a `--enable-X` flag at all?** **Lean: YES, file as `--enable-attachment-risk` with default-OFF for the first PR cycle, then T7 flips it ON via the same rubric.** Rationale: the live-wired path from PR #234 has no operator off-switch today; that asymmetry is a deployability surprise. T7's first sub-PR (call it T7a, scoped to **adding the flag with default-OFF**) is the one structural code change v0.2e contains; T7b is the flip-to-ON PR after profiling. If review prefers "never give it a flag, always-on, monitored only," T7 collapses to a single docs-only PR populating the "always-on, monitored" status column in model-stack.md. Resolve at plan-review.
- **OQ-4 — What is "false-positive rate" per adapter?** **Resolved per §profiling rig Stage 3** (per-adapter fixture + per-adapter threshold). Each task PR cites the fixture and threshold it used.
- **OQ-5 — What if a fixture from Stage 3 does not yet exist?** **Lean: best-effort against nearest available fixture + file a follow-up issue inside the same PR.** Do NOT block the wave on fixture authoring. The escape valve is documented in §profiling rig Stage 3.
- **OQ-6 — What if an adapter fails one criterion but passes two?** **Lean: default stays OFF, the failure is recorded in the PR body as a "deferred to v0.3" decision row.** The PR still merges (so the doc + status column are current). The flip itself is omitted. This means a v0.2e Wave-A PR may close without any default flip — that is intentional, the decision is the deliverable, not the flip.
- **OQ-7 — Does the model-stack.md "default-on status" column already exist?** **Lean: NO — T9 adds it as part of the closeout doc sweep.** If review prefers to add the column up-front (Wave-Pre as a docs-only PR), do that; the rest of the plan is unchanged.

---

# TASKS

---

## Task T1 — `--enable-clip-scene` default flip (or deferral)

**Files touched**
- `manual_test_console/server.py` — change argparse default for `--enable-clip-scene` from `False` to `True` (one-line). Update the help text accordingly.
- `docs/manual-test-handbook.md` — append a one-line entry to the cumulative changelog table (column: "default-on at v0.2e"; value: date + PR #).
- (Stage-2 escape valve only if needed) `companion_harness/clip_scene_change.py` — emit `vision_frame.scene_change_score_ms` timing field if not already present.

**Implementation sketch**
1. **Pre-check.** Run `git log --oneline origin/main | head -1` to confirm v0.2a/b/c merged (commit titles per their roadmap tasks). If not, STOP.
2. **Profile per §profiling rig.** Capture GPU footprint, p95 latency, FP rate. Paste raw outputs into a draft PR body.
3. **Decision.** If all 3 criteria pass → proceed to flip. If any fails → PR body records the failure + "default stays OFF, deferred to v0.3" and the argparse line is NOT changed (PR is doc-only).
4. **Flip (if pass).** Edit `manual_test_console/server.py` argparse default for `--enable-clip-scene` to `True`.
5. **Replay-safety check.** Run `pytest tests/test_policy_replay_exact.py -q` under `/raid/yid042/venvs/companion-harness/bin/python3`; paste pass output into PR body.
6. **Doc update.** Append cumulative changelog row to `docs/manual-test-handbook.md`. Update `docs/model-stack.md` row 13 status column to "default-on @ v0.2e" or "deferred @ v0.2e (criterion-X fail: <number>)".
7. **PR body.** Include "## Profiling evidence" (3 sub-sections, raw command + output for each), "## Rollback" (one-line `--no-enable-clip-scene` override + post-merge revert SHA placeholder), "## Replay safety" (test_policy_replay_exact pass output).

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` under canonical venv passes (100%) — this is the hard gate.
- `pytest tests/test_clip_scene_change.py -q` under canonical venv passes (smoke for the adapter itself; unchanged by this PR).
- Manual smoke on b200: `python -m manual_test_console.server` (no `--enable-clip-scene` flag) — confirm via `/healthz` that scene-change scoring is now ON by default.

**Success criterion**
```
# Before flip:
$ python -m manual_test_console.server --help | grep clip-scene
# (shows --enable-clip-scene default=False)

# After flip:
$ python -m manual_test_console.server --help | grep clip-scene
# (shows --enable-clip-scene default=True)

# Replay safety:
$ pytest tests/test_policy_replay_exact.py -q  # 100% pass, identical fixture outcomes

# Rollback path verified:
$ python -m manual_test_console.server --no-enable-clip-scene  # starts in scene-OFF mode
```

**Anchors locked** — Anchor 1 (one PR), 2 (merge-serial), 3 (profiling evidence), 4 (rollback note), 5 (no new code), 6 (replay safety).

**Cross-references** — model-stack.md row 13; PR #248 (adapter landing); PR #255 (CLI flag landing); §profiling rig in this plan.

**Blocker dependencies** — v0.2a + v0.2b + v0.2c merged. Previous Wave-A PRs (T-prior) merged (Anchor 2: merge-serial).

---

## Task T2 — `--enable-av-conflict` default flip (or deferral)

**Files touched**
- `manual_test_console/server.py` — argparse default for `--enable-av-conflict` from `False` to `True` (if pass).
- `docs/manual-test-handbook.md` — cumulative changelog row.
- `docs/model-stack.md` — row 14 default-on status column.
- (Stage-2 escape valve) `companion_harness/av_conflict_scorer.py` — emit `vision_frame.audio_visual_conflict_ms` timing field if missing.

**Implementation sketch**
1. Pre-check Wave-Pre merge state.
2. Profile per §profiling rig (CPU-only adapter: use RSS delta < 500 MiB for footprint criterion).
3. Decision: flip if all 3 pass; defer if any fails.
4. Flip + doc update + replay-safety check as in T1.
5. PR body: same 3 mandatory sections.

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` — 100% pass.
- `pytest tests/test_av_conflict_scorer.py -q` — 100% pass (unchanged by this PR).
- Manual smoke on b200: `/healthz` confirms av-conflict ON by default (if flipped).

**Success criterion** — analogous to T1, substituting `--enable-av-conflict`.

**Anchors locked** — Anchor 1–6.

**Cross-references** — model-stack.md row 14; PR #242; §profiling rig.

**Blocker dependencies** — Wave-Pre merged; prior Wave-A PR merged (merge-serial).

---

## Task T3 — `--enable-deictic` default flip (or deferral)

**Files touched**
- `manual_test_console/server.py` — argparse default for `--enable-deictic`.
- `docs/manual-test-handbook.md` — cumulative changelog row.
- `docs/model-stack.md` — row 15 default-on status column.
- (Stage-2 escape valve) `companion_harness/deictic_detector_minicpm.py` — emit `vision_frame.deictic_reference_ms` timing field if missing.

**Implementation sketch**
1. Pre-check Wave-Pre merge state.
2. Profile per §profiling rig. **Note:** the deictic detector reuses the foreground MiniCPM instance per PR #246; profile both the "instance reused" path (expected p95 add ≈ one chat() call) AND the "instance unavailable, falls back to null" path. The footprint criterion is satisfied automatically if reuse-path is the only live path (zero additional GPU resident).
3. Decision: flip if all 3 pass. **Caveat:** deictic's FP rate is the most likely failure mode (declarative speech mis-classed as pointing). If FP > 0.05 on `deictic_no_pointing_001`, defer.
4. Flip + doc update + replay-safety check.
5. PR body: same 3 mandatory sections.

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` — 100% pass.
- `pytest tests/test_deictic_detector_minicpm.py -q` — 100% pass.

**Success criterion** — analogous to T1, substituting `--enable-deictic`.

**Anchors locked** — Anchor 1–6.

**Cross-references** — model-stack.md row 15; PR #246; §profiling rig.

**Blocker dependencies** — Wave-Pre merged; prior Wave-A PR merged.

---

## Task T4 — `--enable-urgency` default flip (or deferral)

**Files touched**
- `manual_test_console/server.py` — argparse default for `--enable-urgency`.
- `docs/manual-test-handbook.md` — cumulative changelog row.
- `docs/model-stack.md` — row 16 default-on status column.
- (Stage-2 escape valve) `companion_harness/urgency_scorer.py` — emit `addressing_classified.urgency_score_ms` timing field if missing.

**Implementation sketch**
1. Pre-check Wave-Pre merge state.
2. Profile per §profiling rig (CPU-only adapter: RSS rubric).
3. Decision: flip if all 3 pass. **Caveat:** the urgency scorer's 13-phrase lexicon is the obvious FP risk (any colloquial use of a distress phrase). Use `urgency_calm_speech_001` strictly and record the FP rate verbatim.
4. Flip + doc update + replay-safety check.
5. PR body: same 3 mandatory sections.

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` — 100% pass.
- `pytest tests/test_urgency_scorer.py -q` — 100% pass.

**Success criterion** — analogous to T1, substituting `--enable-urgency`.

**Anchors locked** — Anchor 1–6.

**Cross-references** — model-stack.md row 16; PR #244; §profiling rig.

**Blocker dependencies** — Wave-Pre merged; prior Wave-A PR merged.

---

## Task T5 — `--enable-grounding` default flip (or deferral)

**Files touched**
- `manual_test_console/server.py` — argparse default for `--enable-grounding`.
- `docs/manual-test-handbook.md` — cumulative changelog row.
- `docs/model-stack.md` — row 17 default-on status column.
- (Stage-2 escape valve) `companion_harness/grounding_dino.py` — emit `vision_frame.grounding_confidence_ms` timing field if missing.

**Implementation sketch**
1. Pre-check Wave-Pre merge state.
2. Profile per §profiling rig. **GPU rubric:** grounding-dino-tiny is the largest single GPU adapter in this set (~500 MiB resident per HuggingFace card). Verify against the < 1 GiB ceiling explicitly.
3. Decision: flip if all 3 pass. **Caveat:** open-vocabulary grounding is FP-prone on unknown nouns. Use `grounding_unknown_object_001` strictly.
4. Flip + doc update + replay-safety check.
5. PR body: same 3 mandatory sections.

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` — 100% pass.
- `pytest tests/test_grounding_dino.py -q` — 100% pass.

**Success criterion** — analogous to T1, substituting `--enable-grounding`.

**Anchors locked** — Anchor 1–6.

**Cross-references** — model-stack.md row 17; PR #250; §profiling rig.

**Blocker dependencies** — Wave-Pre merged; prior Wave-A PR merged.

---

## Task T6 — `--enable-embeddings` default flip (or deferral)

**Files touched**
- `manual_test_console/server.py` — argparse default for `--enable-embeddings`.
- `docs/manual-test-handbook.md` — cumulative changelog row.
- `docs/model-stack.md` — row 18 default-on status column.
- (Stage-2 escape valve) `companion_harness/embedder_sentence_transformer.py` — emit `memory_retrieval_event.embedding_ms` timing field if missing.

**Implementation sketch**
1. Pre-check Wave-Pre merge state.
2. Profile per §profiling rig (CPU-only adapter: RSS rubric; expect ≈ 100 MiB for all-MiniLM-L6-v2).
3. Decision: flip if all 3 pass. **FP rubric for embeddings is relaxed (0.10) per §profiling rig Stage 3** — retrieval is recall-tolerant; cite this explicitly in the PR body.
4. Flip + doc update + replay-safety check.
5. PR body: same 3 mandatory sections.

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` — 100% pass.
- `pytest tests/test_embedder_sentence_transformer.py -q` — 100% pass.

**Success criterion** — analogous to T1, substituting `--enable-embeddings`.

**Anchors locked** — Anchor 1–6.

**Cross-references** — model-stack.md row 18; PR #247; §profiling rig.

**Blocker dependencies** — Wave-Pre merged; prior Wave-A PR merged.

---

## Task T7 — AttachmentRiskMonitor posture (OQ-3 dependent)

**Files touched (variant A — flag-then-flip, two sub-PRs)**
- **T7a (structural, one-time):** `manual_test_console/server.py` — add `--enable-attachment-risk` argparse option, default `False`. Update `manual_test_console/live_pipeline.py` to gate the monitor wiring on this flag (the wiring already exists per PR #234; this adds the off-switch). Update `docs/manual-test-handbook.md` to mention the new flag.
- **T7b (flip-or-defer):** same shape as T1–T6 — flip default if profiling passes.

**Files touched (variant B — always-on, monitored only; OQ-3 resolved against the flag)**
- T7 collapses to a single docs-only PR: `docs/model-stack.md` row 11 status column → "always-on, monitored (no operator off-switch by design)". No code change.

**Implementation sketch**
1. **Pre-step.** Resolve OQ-3 at plan-review. If Variant B, skip the rest of this task.
2. **Variant A — T7a:** add the flag in OFF posture. Cite PR #234 in the body (wiring already exists; this is a one-screen addition: argparse line + one `if args.enable_attachment_risk:` gate in `live_pipeline.py`). Replay-safety: with the flag OFF, the live pipeline reverts to the pre-#234 `attachment_risk_level=0.0` default — this **may** change replay outcomes for fixtures recorded post-#234. Audit explicitly: run `pytest tests/test_policy_replay_exact.py -q` with the flag OFF; if any fixture fails, the fixture is updated (recorded against the new default) in the same PR.
3. **Variant A — T7b:** profile per §profiling rig (event-stream heuristic; expect near-zero GPU and near-zero latency). If FP rate on `attachment_risk_neutral_001` < 0.05, flip default to ON. Replay-safety check as usual.

**Test plan**
- Variant A: `pytest tests/test_policy_replay_exact.py -q` passes both before and after each sub-PR.
- Variant A T7a: `pytest tests/test_attachment_risk_monitor.py -q` passes.
- Variant B: no test surface (docs-only).

**Success criterion**
- Variant A: argparse exposes `--enable-attachment-risk`; T7b flips default to ON; replay fixtures green.
- Variant B: model-stack.md row 11 status column populated; PR description records the OQ-3 resolution and rationale.

**Anchors locked** — Anchor 1 (T7a + T7b are still two separate PRs per Anchor 1; the wave admits up to 8 PRs total because T8 may be deferred — see §0).

**Cross-references** — model-stack.md row 11; PR #234; §profiling rig.

**Blocker dependencies** — Wave-Pre merged; OQ-3 resolved; prior Wave-A PR merged.

---

## Task T8 — `--enable-diarization` default flip (or deferral) — GATED ON v0.2b

**Files touched**
- `manual_test_console/server.py` — argparse default for `--enable-diarization` (added by v0.2b Task 11).
- `docs/manual-test-handbook.md` — cumulative changelog row.
- `docs/model-stack.md` — new diarization row (added by v0.2b) default-on status column.

**Implementation sketch**
1. **Hard gate.** Confirm v0.2b merged. If not, this task slips to v0.3.
2. Profile per §profiling rig. **GPU rubric:** pyannote-3.1 is ~300 MiB; well under 1 GiB.
3. **p95 latency rubric:** the v0.2b roadmap gate is `diarization_latency_ms_p95 < 50ms` (per roadmap numeric gates table). Reuse that measurement verbatim — do not re-measure.
4. **FP rubric:** the v0.2b roadmap gate is `diarization_false_speaker_change_rate < 0.05` on `diarization_acoustic_feedback_001`. Reuse that measurement verbatim.
5. Decision: flip if all 3 pass. **Caveat:** if v0.2b shipped `diarization_latency_ms_p95` measured at exactly 50.0 ms on b200, the default stays OFF (boundary is exclusive). Record the exact number.
6. **Special replay-safety concern:** flipping diarization ON changes `current_speaker_id` from `None` to a real speaker label, which **does** affect the v0.1k speaker-continuity tie-breaker (roadmap Anchor 1 + v0.2b Task 9). The replay-safety check therefore must use the **v0.1k** POLICY_VERSION fixtures, not the v0.1j ones. Use both fixture sets; the v0.1j set must remain bit-identical (proof that the off-path is unchanged), and the v0.1k set must remain bit-identical (proof that the on-path is unchanged from v0.2b's landing baseline).
7. Flip + doc update.

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` — 100% pass against BOTH v0.1j and v0.1k fixture pins.
- `pytest tests/test_pyannote_diarization_adapter.py -q` — 100% pass.

**Success criterion** — analogous to T1, substituting `--enable-diarization`. Plus: v0.1j AND v0.1k replay fixtures both green.

**Anchors locked** — Anchor 1–6.

**Cross-references** — model-stack.md (new diarization row, added by v0.2b); v0.2b Task 8 + Task 11; roadmap-v0.2-draft.md Anchor 1 + Anchor 3 + numeric gates table; §profiling rig.

**Blocker dependencies** — v0.2b merged (hard gate); Wave-Pre merged; prior Wave-A PR merged.

---

## Task T9 — Closeout doc sweep

**Files touched**
- `docs/model-stack.md` — add a new column "default-on status (v0.2e)" to the "Real models (operational)" table if T1's PR did not already (OQ-7). Populate every row with one of: `default-on @ v0.2e`, `deferred @ v0.2e (criterion-X: <number>)`, `out of scope @ v0.2e`. The "out of scope" rows are: BackgroundReasoner (different rollout regime), MiniCPM-o foreground (the proposal model, not an opt-in adapter), Kokoro TTS / MiniCPM-o native TTS (governed by `--tts-adapter`, not `--enable-X`), Silero VAD / SmartTurn / Backchannel classifier / MiniCPM addressing classifier / Wake-word / MiniCPM-o native_duplex EOU / Whisper-tiny ASR (default-on already; never gated by `--enable-X`).
- `docs/manual-test-handbook.md` — append a "v0.2e summary" section listing each adapter's final disposition with PR links.
- `docs/roadmap-v0.2-draft.md` — update `adapter_default_on_readiness_count` informational gate row in the gates table to its final value (count of adapters with complete profiling evidence; not necessarily count flipped).

**Implementation sketch**
1. Confirm all Wave-A PRs (T1–T8) have either merged or been formally deferred (deferral is a merged docs-only PR per OQ-6).
2. Populate the new "default-on status (v0.2e)" column in `docs/model-stack.md`.
3. Write the "v0.2e summary" section in `docs/manual-test-handbook.md` — one row per adapter, columns: adapter, final default, PR #, key blocker if deferred.
4. Update the roadmap gate row.
5. PR body: cross-link to every Wave-A PR.

**Test plan**
- No new test surface. Existing tests must remain green:
  - `pytest tests/test_policy_replay_exact.py -q` — 100% pass under canonical venv.
  - `pytest tests/ -q` — full suite passes (regression smoke).

**Success criterion**
```
$ grep -c "default-on @ v0.2e\|deferred @ v0.2e\|out of scope @ v0.2e" docs/model-stack.md
# (number == count of rows in "Real models (operational)" table)

$ grep -A 20 "v0.2e summary" docs/manual-test-handbook.md
# (table with one row per Wave-A adapter)
```

**Anchors locked** — Anchor 1 (closeout is its own PR), 5 (no new code).

**Cross-references** — every Wave-A PR; model-stack.md; manual-test-handbook.md; roadmap-v0.2-draft.md gates table.

**Blocker dependencies** — every Wave-A PR (T1–T8) merged or formally deferred.

---

## §Numeric gates

| Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|
| `per_adapter_gpu_footprint_MiB` | Wave-A task owner per adapter | `nvidia-smi --query-gpu=memory.used` delta between `--enable-X` OFF and ON, after 60s warm-up + one sample interaction | 1 sample per adapter (single b200 session) | blocking — adapter cannot flip if > 1024 MiB AND not CPU-only |
| `per_adapter_cpu_rss_MiB` | Wave-A task owner per adapter | `ps -o rss=` delta on `manual_test_console.server` process (CPU-only adapters only) | 1 sample per adapter | blocking — adapter cannot flip if > 500 MiB additional RSS |
| `per_adapter_p95_latency_ms_delta` | Wave-A task owner per adapter | `FixtureScenarioDriver` replay of `clean_speech_chunked_001` twice (off/on), p95 diff of per-call duration field per §profiling rig Stage 2 table | ≥ 1 replay each posture (fixture provides ≥ 100 events) | blocking — adapter cannot flip if delta ≥ 50 ms |
| `per_adapter_false_positive_rate` | Wave-A task owner per adapter | Per-adapter fixture replay per §profiling rig Stage 3 table, FP / total | All events in fixture | blocking — adapter cannot flip if rate exceeds per-adapter threshold (vision/urgency/attachment-risk: < 0.05; embeddings: < 0.10; diarization: < 0.05 via existing roadmap gate) |
| `per_adapter_replay_safety_pass_rate` | Wave-A task owner per adapter | `pytest tests/test_policy_replay_exact.py -q` under canonical venv, before AND after the flip | All fixtures pinning POLICY_VERSION | blocking — 100% required, no exceptions |
| `adapter_default_on_readiness_count` | T9 owner | Count of Wave-A adapters with all 3 profiling sub-sections populated in their PR body (regardless of flip vs defer) | One per adapter (8 total in scope) | informational — must equal 7 or 8 depending on T8 gate outcome |
| `wave_2e_flip_count` | T9 owner | Count of Wave-A adapters whose argparse default actually flipped to ON | One per adapter | informational — no target; the decision is the deliverable |
| `wave_2e_rollback_count_30d` | T9 owner + operator (30 days post-merge) | Count of `--no-enable-X` overrides observed in operator session breadcrumbs over the 30-day window post-flip | All sessions in window | informational — high count signals a flip should be reverted in v0.3 |

---

## §Out of scope

- BackgroundReasoner default flip (different rollout regime per roadmap Anchor 2; gated on `background_reasoner_budget_exhaustion_rate < 0.05` over 24h production use).
- TTS adapter default selection (`--tts-adapter` semantics; gated on libcudart resolution per issue #157).
- New profiling fixtures beyond what §profiling rig names (file follow-up issues; do not block wave).
- New evaluation metrics beyond the 3-criterion rubric (additional metrics are v0.3 work).
- Multi-host or production deployment of any flipped default (v0.3 per roadmap Anchor 5).
- POLICY_VERSION bumps (none in v0.2e; v0.1k lands with v0.2b, v0.2-final lands with v0.2 Wave 6 Task 20).

---

## §Risks

1. **Wave-Pre slip.** If v0.2a/b/c slip, v0.2e slips proportionally. T8 is the most exposed (gated on v0.2b's pyannote work). **Mitigation:** T1–T7 do not gate on v0.2b; they can proceed when v0.2a + v0.2c merge (v0.2b can lag without blocking 7/8 of the wave).
2. **b200 profiling-slot contention.** Eight authors may want b200 simultaneously. **Mitigation:** soft-schedule via the project memory channel; profiling is ≈ 10 min per adapter and serializable in batch.
3. **FP fixture authoring overhead.** Eight per-adapter FP fixtures may not all exist when Wave-A starts. **Mitigation:** §profiling rig Stage 3 escape valve (best-effort + follow-up issue). This is the wave's load-bearing flexibility.
4. **Replay-safety regression post-flip.** A flipped default may cause a fixture to fail in a way that only manifests under multi-event sequences not in the per-task replay-safety check. **Mitigation:** Anchor 6 + Anchor 2 (merge-serial) ensure single-adapter bisect; the 30-day rollback count gate catches drift.
5. **OQ-3 ambiguity (attachment-risk posture).** Without OQ-3 resolution, T7 is two PRs (Variant A) or one PR (Variant B). **Mitigation:** resolve at plan-review; T7's two-variant structure is explicit so either path is concrete.
6. **Diarization boundary value.** If v0.2b lands with `diarization_latency_ms_p95` exactly at 50.0 ms (boundary), T8 defers. **Mitigation:** roadmap gate is `< 50ms` (strict), so the boundary case is well-defined.
7. **Documentation drift across the wave.** Eight parallel-authored PRs may each insert a slightly different changelog-row format. **Mitigation:** Anchor 3 + Anchor 4 specify mandatory section names; T9 closeout PR canonicalizes the wording.

---

## §Cross-references

- Source roadmap: `docs/roadmap-v0.2-draft.md` (Wave 5 / Task 16; Anchor 5 production-deployability deferral).
- Model-stack reference: `docs/model-stack.md` (rows 11, 13–18; "Real models (operational)" table).
- Live pipeline: `manual_test_console/live_pipeline.py` (opt-in adapter factory pattern).
- Live entry point: `manual_test_console/server.py` (argparse `--enable-*` flags; current main has `--enable-vision` + `--enable-live-pipeline`; PR #255 added 6 more; v0.2b will add `--enable-diarization`).
- Sibling plans (waves this gates on): `docs/plan-v0.2a-execution.md`, `docs/plan-v0.2b-execution.md`, `docs/plan-v0.2c-execution.md`.
- Spec: `docs/architecture-v0.1.md` (FROZEN; invariants #5 replay determinism + #8 silence wins ties + #10 EventLogger async — all relevant to per-adapter FP and replay safety).
- Project discipline: `CLAUDE.md` (coding rule 3 surgical edits — this wave is the canonical small-PR rule applied at maximum fidelity).
