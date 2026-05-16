# Plan — v0.2e Execution (Adapter default-on decision wave)

## Status: **DRAFT** — drafted 2026-05-16. Source roadmap: `docs/roadmap-v0.2-draft.md` Wave 5 (Task 16). Not yet plan-critic'd; not yet sub-agent dispatched.

> v0.2e is the **per-adapter default-on review wave** for the opt-in real adapters that landed across v0.1j and the post-Round-4 sweep (PR #255 + #260/#262/#263/#264/#271 + diarization once v0.2b lands). Each adapter is evaluated on a fixed 3-criterion rubric (GPU footprint, p95 latency contribution, false-positive rate). Where all three pass, `--enable-X` is flipped to default-ON in `manual_test_console/server.py` via a **single tiny PR per adapter**. Where any criterion fails, default stays OFF and the failure is recorded in the PR body for re-evaluation in v0.3.
>
> v0.2e ships **no new model code, no new policy code, no new contract surface**. It is a measurement-and-config wave: profiling + decision + one-line argparse default flip per adapter. Each PR is independently revertible (one-line `--no-enable-X` operator override, plus a clean revert SHA).
>
> Pure docs-and-defaults discipline: every flip cites profiling evidence in the PR body. No flip ships without numbers.

---

## §0 Dependency on v0.2a / v0.2b / v0.2c

**v0.2e cannot start until v0.2a and v0.2c are merged.** v0.2b (diarization) is NOT a gate — diarization is out of scope for v0.2e and its default-on evaluation is deferred to v0.3. Each wave contributes adapters that v0.2e measures:

- **v0.2a (Real BackgroundReasoner — roadmap Wave 1)** — does NOT add a new opt-in adapter under measurement here. Listed for completeness; v0.2e excludes BackgroundReasoner from the per-adapter rubric (its rollout is governed by `background_reasoner_budget_exhaustion_rate < 0.05` per roadmap Anchor on `BACKGROUND_REASONER` flag default, not by the GPU/latency/false-positive rubric in this plan).
- **v0.2b (Real diarization — roadmap Wave 2)** — adds `--enable-diarization` (roadmap Task 11). v0.2e does NOT include diarization; its default-on evaluation defers to v0.3 (see BLOCKER 1 reconciliation: v0.2e is reconciled to the 6 adapters in the v0.2 roadmap Wave 5 "max 6 PRs" scope).
- **v0.2c (Real benchmark data loaders — roadmap Wave 3)** — does NOT add a runtime opt-in adapter. Listed for completeness; out of scope here.

**Why this gate is hard, not soft:** v0.2e's measurement targets must exist in the live pipeline before they can be profiled. Flipping a default for a flag that doesn't yet exist is meaningless. v0.2b slip does not affect v0.2e (diarization is out of scope here); the other 6 tasks proceed once v0.2a + v0.2c merge.

The 6 adapters in scope, sourced from `docs/model-stack.md` § "Real models (operational)" rows 13–18 (matching the 6 `--enable-*` flags present in the live CLI and the v0.2 roadmap Wave 5 "max 6 PRs" constraint). AttachmentRiskMonitor and PyannoteDiarizationAdapter are excluded from v0.2e (see §Open Questions OQ-3 and the v0.3 deferral note):

| # | Adapter | CLI flag | Backend | Model-stack row |
|---|---|---|---|---|
| 1 | `CLIPSceneChangeScorer` | `--enable-clip-scene` | CLIP-base on CPU | row 13 |
| 2 | `HeuristicAVConflictScorer` | `--enable-av-conflict` | OpenCV+VAD CPU | row 14 |
| 3 | `MiniCPMDeicticDetector` | `--enable-deictic` | MiniCPM-o text-only chat | row 15 |
| 4 | `ProsodyLexiconUrgencyScorer` | `--enable-urgency` | lexicon+prosody CPU | row 16 |
| 5 | `GroundingDINOAdapter` | `--enable-grounding` | grounding-dino-tiny CPU (adapter defaults to CPU; GPU optional) | row 17 |
| 6 | `SentenceTransformerEmbedder` | `--enable-embeddings` | all-MiniLM-L6-v2 CPU | row 18 |

AttachmentRiskMonitor (`--enable-attachment-risk`) and PyannoteDiarizationAdapter (`--enable-diarization`) are deferred to v0.3. AttachmentRiskMonitor's flag posture (OQ-3) and its rubric evaluation are v0.3 work. Diarization evaluation depends on v0.2b landing and is also v0.3 work. Neither flag exists in the live CLI today; v0.2 roadmap Wave 5 is "max 6 PRs" matching these 6 entries.

---

## §Dependency graph

```
┌──────────────────────────────────────────────────────────────────────┐
│ Wave-Pre (gates — must merge before any v0.2e PR opens):            │
│   v0.2a Tasks 1-6 merged                                             │
│   v0.2c Tasks 12-14 merged                                           │
│   (v0.2b diarization is NOT a gate here — deferred to v0.3)         │
└─────────┬────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Wave-A (per-adapter profiling + decision; max 6-way parallel)       │
│   T1 CLIP scene change                                               │
│   T2 AV conflict                                                     │
│   T3 deictic                                                         │
│   T4 urgency                                                         │
│   T5 grounding DINO                                                  │
│   T6 sentence-transformer embeddings                                 │
└─────────┬────────────────────────────────────────────────────────────┘
          │
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Wave-B (closeout):                                                   │
│   T7 cumulative changelog row in manual-test-handbook.md +           │
│       model-stack.md "default-on status" column populated            │
└──────────────────────────────────────────────────────────────────────┘
```

---

## §Concurrency map

| Wave | Concurrency | Tasks | Notes |
|---|---|---|---|
| Pre | sequential gate | v0.2a + v0.2c | Hard gate; v0.2e does not open any PR until these merge. v0.2b (diarization) is NOT a gate for v0.2e — diarization is out of scope and deferred to v0.3. |
| A | **6×** (one agent per adapter) | T1, T2, T3, T4, T5, T6 | Each task is one PR. No shared file edits — each PR touches `manual_test_console/server.py` argparse default at a different line, plus the PR body's profiling block. **Anchor: one-at-a-time merge (not one-at-a-time author)** — OQ-2 below resolves that PRs are *authored* in parallel but *merged serially* so any regression bisects cleanly to one adapter flip. |
| B | sequential | T7 | Closeout doc sweep — must come after every Wave-A PR merges (or defers). |

**Max realistic concurrency: 6 agents** (Wave-A fan-out). **Realistic gate: review bandwidth + b200 profiling slot scheduling** (each agent needs ~10 min on b200 for profiling — see §profiling rig).

---

## §Profiling rig (shared substrate; not a code task)

Every Wave-A task uses the same 3-stage profiling recipe. Recording this here so each task references it instead of re-defining it.

**Stage 1 — GPU footprint measurement.**
- Run `manual_test_console/server.py --enable-X` (no other adapters enabled) under canonical venv on b200.
- After 60s of warm-up + a single sample interaction, capture `nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits` and subtract the baseline (`--enable-X` OFF) reading taken in the same session.
- Record delta in MiB; pass criterion: **< 1024 MiB OR adapter is CPU-only (delta == 0)**.
- For CPU-only adapters (rows 1, 2, 4, 6 above), capture RSS via `ps -o rss= -p <server_pid>` instead and record in MiB; pass criterion is "< 800 MiB additional RSS" for the CPU rubric line. (CLIP-base loads ~600 MiB into RAM per the adapter file; the 800 MiB ceiling gives margin while still gating against runaway loads.)

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
  - Attachment-risk → out of scope for v0.2e (deferred to v0.3)
  - Diarization → out of scope for v0.2e (deferred to v0.3)
- If the field name is not yet emitted in main, the task PR description records "field-name TBD; using wall-clock around `<exact call site>` instead" and the task owner adds the timing field in the same PR as a one-line emission. This is the **Stage-2 escape valve** — the only out-of-scope code change Wave-A permits under Anchor 5's explicit exception; everything else is config-only. The PR body MUST cite this escape valve by name to signal to reviewers that the code addition is intentional and bounded. (See also §Foundation-first ordering Stage 0 note for the preferred alternative: a dedicated instrumentation PR before Wave-A begins.)

**Stage 3 — false-positive rate measurement.**
- Per-adapter ground-truth fixture from `docs/model-stack.md` test corpus:
  - CLIP scene change → `clip_scene_no_change_001` (a single static scene; expected score < 0.2; FP = score crosses any policy gate threshold)
  - AV conflict → `av_no_conflict_001` (aligned audio+video; FP = `audio_visual_conflict_score > 0.5`)
  - Deictic → `deictic_no_pointing_001` (declarative speech, no deictic; FP = `deictic_reference=True` or `deictic_ambiguous=True`)
  - Urgency → `urgency_calm_speech_001` (non-distress phrases; FP = `urgency_score > 0.5`)
  - Grounding DINO → `grounding_unknown_object_001` (vocab miss; FP = `grounding_confidence > 0.5`)
  - Embeddings → `embeddings_unrelated_query_001` (query unrelated to corpus; FP = top-1 cosine > 0.7 against unrelated memory)
  - Attachment-risk → out of scope for v0.2e (deferred to v0.3)
  - Diarization → out of scope for v0.2e (deferred to v0.3)
- Each fixture is replayed under canonical venv on b200; the FP rate is `(FP events) / (total relevant events)`.
- Pass criterion is per-adapter:
  - Vision / urgency: **< 0.05** (1-in-20 false positives, in line with `dataset_row_skip_rate` precedent)
  - Embeddings: **< 0.10** (retrieval is recall-tolerant; FP cost is "retrieved an irrelevant memory" rather than "triggered a bad action")
- Each task PR cites the exact fixture path AND the exact pass threshold it used.

**Fixtures that do not yet exist** are filed as a one-line follow-up issue inside the task PR body. The PR can still flip the default if Stages 1+2 pass AND a "best-effort" FP measurement is provided against the nearest available fixture (with explicit "fixture #XXX needs to exist for a clean re-eval" note). This is the **escape valve** for the wave — without it, fixture authoring becomes a v0.2e blocker, which it is not.

---

## §Foundation-first ordering

**Wave-Pre (v0.2a/b/c) is a blocking gate.** No v0.2e PR opens until those waves merge. The profiling rig (§profiling rig above) is documentation-only — it lives in this plan, not in code, so no PR is needed to establish it. Each Wave-A task PR cites this §profiling rig section by name in its description.

**Stage 0 — Instrumentation prerequisite.** The per-adapter timing fields listed in §profiling rig Stage 2 (e.g., `vision_frame.scene_change_score_ms`, `memory_retrieval_event.embedding_ms`) do NOT exist on main today. Before any Wave-A flip PR runs, one of two things must be true: (a) a dedicated instrumentation PR adds these fields to the relevant adapter files (recommended — keeps flip PRs config-only and satisfies Anchor 5), OR (b) the field is added inside the flip PR under the explicit Stage-2 escape valve (§profiling rig Stage 2, last paragraph), with the one-line emission cited in the PR body's profiling section. Option (a) is preferred because it keeps Wave-A PRs strictly config-only. If option (b) is chosen, the PR description MUST name the escape valve explicitly so reviewers understand why code exists in a nominally config-only PR. This resolves the apparent contradiction between Anchor 5 ("no new code paths") and the Stage-2 escape valve: the escape valve is an explicit, named exception to Anchor 5, not a loophole.

**FP fixture alignment note.** The FP fixture names in §profiling rig Stage 3 (`clip_scene_no_change_001`, `av_no_conflict_001`, etc.) are not yet confirmed against the v0.2 roadmap fixture manifest. Each Wave-A task owner must verify whether their fixture exists before profiling; if it does not, use the §profiling rig Stage 3 escape valve (best-effort + follow-up issue). A separate PR to add missing fixtures to the v0.2 roadmap fixture manifest is recommended before Wave-A fan-out begins.

---

## §Anchors (locked)

- **Anchor 1 — One PR per adapter flip.** No batch flips. Each adapter is its own revertible unit. Even if all adapters pass in the same week, they ship as separate PRs (max 6 in Wave-A, plus 1 closeout = 7 total in v0.2e).
- **Anchor 2 — One-at-a-time merge.** PRs may be **authored in parallel** (Wave-A fan-out), but they merge **serially** so that any post-flip regression bisects to a single adapter. The author of a flipped PR rebases on `main` after the previous PR merges.
- **Anchor 3 — Profiling evidence in PR body is mandatory.** A PR without numbers in the body is auto-rejected by review. The PR body MUST contain a "## Profiling evidence" section with three sub-sections (GPU/CPU footprint, p95 latency, false-positive rate) and the exact command + output for each.
- **Anchor 4 — Rollback note in PR body is mandatory.** Every PR body MUST contain a "## Rollback" section naming **both** (a) the operator-side one-line `--no-enable-X` flag override that returns to the OFF posture without a revert AND (b) the revert SHA (filled in post-merge by the merger). Without (a) and (b), no flip.
- **Anchor 5 — No new code paths.** v0.2e PRs change at most: `manual_test_console/server.py` argparse defaults, `docs/manual-test-handbook.md` cumulative changelog, `docs/model-stack.md` "default-on status" column. The Stage-2 escape valve (one-line timing-emission patch when an event field is missing) is the **only** out-of-scope code edit allowed, and only if explicitly cited in the PR body's profiling section.
- **Anchor 6 — Tier-B replay safety.** Every PR must include the result of `pytest tests/test_policy_replay_exact.py -q` under canonical venv showing 100% pass before and after the flip. A default flip changes the **default** PolicyInputs distribution but must NOT change a single recorded fixture's replay outcome (invariant #5). If a fixture fails, the flip is rolled back and the fixture is investigated as a regression.

---

## §Open Questions (with leans)

- **OQ-1 — Which adapters are in scope?** **Resolved: the 6 adapters** matching the v0.2 roadmap Wave 5 "max 6 PRs" scope and the 6 `--enable-*` flags present in the live CLI (T1–T6 above). AttachmentRiskMonitor and PyannoteDiarizationAdapter are deferred to v0.3 (OQ-3 and diarization gating respectively). Excludes BackgroundReasoner (different rollout regime per roadmap Anchor 2) and TTS adapters (native MiniCPM TTS is opt-in per `--tts-adapter`, not per `--enable-X`; its default flip is a separate v0.3 decision tied to libcudart resolution).
- **OQ-2 — Author-parallel or merge-parallel?** **Resolved as Anchor 2: author-parallel, merge-serial.** Rationale: 6-way author parallelism unlocks throughput; merge-serial preserves clean bisect when a flipped default later turns up a regression in a multi-day manual test.
- **OQ-3 — Does AttachmentRiskMonitor get a `--enable-X` flag at all?** **Deferred to v0.3.** The live-wired path from PR #234 has no operator off-switch today, but resolving that asymmetry is not in v0.2e's 6-adapter scope. The flag posture question (always-on-monitored vs explicit `--enable-attachment-risk`) is recorded here for v0.3 planning.
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
- (Stage-2 escape valve only if needed) `companion_harness/clip_scene_scorer.py` — emit `vision_frame.scene_change_score_ms` timing field if not already present. (Filename confirmed: `clip_scene_scorer.py` is the actual name on main. `docs/model-stack.md` still references the old filename `clip_scene_change.py`; a separate cleanup PR should update that path. R2 finding on stale filename was not applicable to the plan — the plan uses the correct filename.)

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
- `pytest tests/test_clip_scene_scorer.py -q` under canonical venv passes (smoke for the adapter itself; unchanged by this PR).
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

**Blocker dependencies** — v0.2a + v0.2c merged (Wave-Pre gate). v0.2b is NOT a blocker for v0.2e. Previous Wave-A PRs (T-prior) merged (Anchor 2: merge-serial).

---

## Task T2 — `--enable-av-conflict` default flip (or deferral)

**Files touched**
- `manual_test_console/server.py` — argparse default for `--enable-av-conflict` from `False` to `True` (if pass).
- `docs/manual-test-handbook.md` — cumulative changelog row.
- `docs/model-stack.md` — row 14 default-on status column.
- (Stage-2 escape valve) `companion_harness/av_conflict_scorer.py` — emit `vision_frame.audio_visual_conflict_ms` timing field if missing.

**Implementation sketch**
1. Pre-check Wave-Pre merge state.
2. Profile per §profiling rig (CPU-only adapter: use RSS delta < 800 MiB for footprint criterion per §profiling rig Stage 1).
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
2. Profile per §profiling rig (CPU-only adapter: RSS delta < 800 MiB per §profiling rig Stage 1).
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
- (Stage-2 escape valve) `companion_harness/grounding_dino_adapter.py` — emit `vision_frame.grounding_confidence_ms` timing field if missing. (Filename confirmed: `grounding_dino_adapter.py` is the actual name on main. `docs/model-stack.md` still references the old filename `grounding_dino.py`; a separate cleanup PR should update that path. R2 finding on stale filename was not applicable to the plan — the plan uses the correct filename.)

**Implementation sketch**
1. Pre-check Wave-Pre merge state.
2. Profile per §profiling rig. **CPU/RSS rubric:** the actual `grounding_dino_adapter.py` defaults to CPU; use RSS delta < 800 MiB per §profiling rig Stage 1 (same CPU path as other CPU-only adapters). If the deployment runs with `device=cuda`, also capture the GPU delta and verify against the < 1 GiB ceiling. Record which device was active in the PR body's profiling section.
3. Decision: flip if all 3 pass. **Caveat:** open-vocabulary grounding is FP-prone on unknown nouns. Use `grounding_unknown_object_001` strictly.
4. Flip + doc update + replay-safety check.
5. PR body: same 3 mandatory sections.

**Test plan**
- `pytest tests/test_policy_replay_exact.py -q` — 100% pass.
- `pytest tests/test_grounding_dino_adapter.py -q` — 100% pass.

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
2. Profile per §profiling rig (CPU-only adapter: RSS delta < 800 MiB per §profiling rig Stage 1; expect ≈ 100 MiB for all-MiniLM-L6-v2, well within the ceiling).
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

## Task T7 — Closeout doc sweep

**Files touched**
- `docs/model-stack.md` — add a new column "default-on status (v0.2e)" to the "Real models (operational)" table if T1's PR did not already (OQ-7). Populate every row with one of: `default-on @ v0.2e`, `deferred @ v0.2e (criterion-X: <number>)`, `out of scope @ v0.2e`. The "out of scope" rows are: BackgroundReasoner (different rollout regime), MiniCPM-o foreground (the proposal model, not an opt-in adapter), Kokoro TTS / MiniCPM-o native TTS (governed by `--tts-adapter`, not `--enable-X`), Silero VAD / SmartTurn / Backchannel classifier / MiniCPM addressing classifier / Wake-word / MiniCPM-o native_duplex EOU / Whisper-tiny ASR (default-on already; never gated by `--enable-X`), AttachmentRiskMonitor (deferred to v0.3; OQ-3 unresolved), PyannoteDiarizationAdapter (deferred to v0.3; depends on v0.2b landing).
- `docs/manual-test-handbook.md` — append a "v0.2e summary" section listing each adapter's final disposition with PR links.
- `docs/roadmap-v0.2-draft.md` — update `adapter_default_on_readiness_count` informational gate row in the gates table to its final value (count of adapters with complete profiling evidence; not necessarily count flipped).

**Implementation sketch**
1. Confirm all Wave-A PRs (T1–T6) have either merged or been formally deferred (deferral is a merged docs-only PR per OQ-6).
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

**Cross-references** — every Wave-A PR (T1–T6); model-stack.md; manual-test-handbook.md; roadmap-v0.2-draft.md gates table.

**Blocker dependencies** — every Wave-A PR (T1–T6) merged or formally deferred.

---

## §Numeric gates

| Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|
| `per_adapter_gpu_footprint_MiB` | Wave-A task owner per adapter | `nvidia-smi --query-gpu=memory.used` delta between `--enable-X` OFF and ON, after 60s warm-up + one sample interaction | 1 sample per adapter (single b200 session) | blocking — adapter cannot flip if > 1024 MiB AND not CPU-only |
| `per_adapter_cpu_rss_MiB` | Wave-A task owner per adapter | `ps -o rss=` delta on `manual_test_console.server` process (CPU-only adapters only) | 1 sample per adapter | blocking — adapter cannot flip if > 800 MiB additional RSS |
| `per_adapter_p95_latency_ms_delta` | Wave-A task owner per adapter | `FixtureScenarioDriver` replay of `clean_speech_chunked_001` twice (off/on), p95 diff of per-call duration field per §profiling rig Stage 2 table | ≥ 1 replay each posture (fixture provides ≥ 100 events) | blocking — adapter cannot flip if delta ≥ 50 ms |
| `per_adapter_false_positive_rate` | Wave-A task owner per adapter | Per-adapter fixture replay per §profiling rig Stage 3 table, FP / total | All events in fixture | blocking — adapter cannot flip if rate exceeds per-adapter threshold (vision/urgency: < 0.05; embeddings: < 0.10) |
| `per_adapter_replay_safety_pass_rate` | Wave-A task owner per adapter | `pytest tests/test_policy_replay_exact.py -q` under canonical venv, before AND after the flip | All fixtures pinning POLICY_VERSION | blocking — 100% required, no exceptions |
| `adapter_default_on_readiness_count` | T7 owner | Count of Wave-A adapters with all 3 profiling sub-sections populated in their PR body (regardless of flip vs defer) | One per adapter (6 total in scope) | informational — must equal 6 when all Wave-A PRs have merged or deferred |
| `wave_2e_flip_count` | T7 owner | Count of Wave-A adapters whose argparse default actually flipped to ON | One per adapter | informational — no target; the decision is the deliverable |
| `wave_2e_rollback_count_30d` | T7 owner + operator (30 days post-merge) | Count of `--no-enable-X` overrides observed in operator session breadcrumbs over the 30-day window post-flip | All sessions in window | informational — high count signals a flip should be reverted in v0.3 |

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

1. **Wave-Pre slip.** If v0.2a/v0.2c slip, v0.2e slips proportionally. v0.2b slip does not affect v0.2e (diarization is out of scope). **Mitigation:** T1–T6 proceed when v0.2a + v0.2c merge; v0.2b can lag without affecting any v0.2e task.
2. **b200 profiling-slot contention.** Six authors may want b200 simultaneously. **Mitigation:** soft-schedule via the project memory channel; profiling is ≈ 10 min per adapter and serializable in batch.
3. **FP fixture authoring overhead.** Six per-adapter FP fixtures may not all exist when Wave-A starts. **Mitigation:** §profiling rig Stage 3 escape valve (best-effort + follow-up issue). This is the wave's load-bearing flexibility.
4. **Replay-safety regression post-flip.** A flipped default may cause a fixture to fail in a way that only manifests under multi-event sequences not in the per-task replay-safety check. **Mitigation:** Anchor 6 + Anchor 2 (merge-serial) ensure single-adapter bisect; the 30-day rollback count gate catches drift.
5. **OQ-3 ambiguity (attachment-risk posture).** AttachmentRiskMonitor is deferred to v0.3; OQ-3 is flagged for v0.3 plan-review. No risk to v0.2e.
6. **Documentation drift across the wave.** Six parallel-authored PRs may each insert a slightly different changelog-row format. **Mitigation:** Anchor 3 + Anchor 4 specify mandatory section names; T7 closeout PR canonicalizes the wording.

---

## §Cross-references

- Source roadmap: `docs/roadmap-v0.2-draft.md` (Wave 5 / Task 16; Anchor 5 production-deployability deferral).
- Model-stack reference: `docs/model-stack.md` (rows 13–18 in scope; row 11 AttachmentRiskMonitor noted as "out of scope @ v0.2e" in closeout column; "Real models (operational)" table).
- Live pipeline: `manual_test_console/live_pipeline.py` (opt-in adapter factory pattern).
- Live entry point: `manual_test_console/server.py` (argparse `--enable-*` flags; current main has `--enable-vision` + `--enable-live-pipeline`; PR #255 added 6 more; v0.2b will add `--enable-diarization`).
- Sibling plans (waves this gates on): `docs/plan-v0.2a-execution.md`, `docs/plan-v0.2b-execution.md`, `docs/plan-v0.2c-execution.md`.
- Spec: `docs/architecture-v0.1.md` (FROZEN; invariants #5 replay determinism + #8 silence wins ties + #10 EventLogger async — all relevant to per-adapter FP and replay safety).
- Project discipline: `CLAUDE.md` (coding rule 3 surgical edits — this wave is the canonical small-PR rule applied at maximum fidelity).
