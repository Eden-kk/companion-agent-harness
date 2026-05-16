# Plan — v0.2d Execution (Eval Phase C live examiner)

## Status: **DRAFT** — drafted 2026-05-16. Source roadmaps: `docs/roadmap-v0.2-draft.md` (Wave 4) + `docs/roadmap-eval-draft.md` (Phase C). Sibling plans (in flight): `docs/plan-v0.2a-execution.md` (BackgroundReasoner), `docs/plan-v0.2b-execution.md` (diarization), `docs/plan-v0.2c-execution.md` (benchmark loaders). Not yet plan-critic'd; not yet sub-agent dispatched.

> v0.2d ships Eval Phase C — the live-examiner case-source surface — but **honestly scoped to v0.2**: framework + a small curated diarized-session fixture pack. Real-mode against arbitrary live operator recordings (full Full-Duplex-Bench v2 partner play) is deferred to v0.3. v0.2d is **gated on v0.2b** (diarization adapter + `current_speaker_id` in PolicyInputs); Wave 1 here does NOT dispatch coders until v0.2b is merged. Phase C in v0.2 satisfies pinned-success-criterion #3 ("Eval Phase C ships both synthetic-mode framework AND a small real-diarized fixture pack (3+ multi-speaker recordings)").

---

## §0 Scope-honesty preamble

The roadmap-eval-draft.md original Phase C definition envisioned an LLM-driven side-process examiner that produces utterances live (`fixture_audio_chunk_injected`) and is scored per-case on Full-Duplex-Bench v2. That full envelope is **not** v0.2 scope. v0.2d ships:

- **Framework:** `LiveExaminerCaseSource` that consumes a *recorded* diarized session (event log + audio + per-utterance ground-truth speaker labels) and emits `EvaluationCase` instances attributed to specific speakers. Default mode is **synthetic**; the case-source builds `EvaluationCase`s from the fixture pack. **Real-mode raises `NotImplementedError`** with a docstring pointer to v0.3.
- **Fixture pack:** ≥3 multi-speaker recordings curated in `tests/fixtures/phase_c/` with per-utterance ground-truth speaker labels and replay-safe event logs.
- **Metrics:** speaker-attributed metric implementations (per-speaker addressing-accuracy, per-speaker turn-gap distribution) + a `replay_match_rate_vs_ground_truth` aggregate.

What v0.2d does **NOT** ship (deferred to v0.3):
- LLM-driven side-process partner producing live utterances.
- Full-Duplex-Bench v2 case-source.
- Self-play (harness-as-examiner of another harness).
- Cross-session speaker recognition.

This split is the §3 anchor of v0.2 roadmap and is restated in roadmap-eval-draft.md (T5 below updates that doc).

---

## §Dependency graph

```
┌────────────────────────────────────────────────────────────────┐
│ EXTERNAL GATES (must merge before v0.2d Wave 1 dispatch)      │
│   v0.2b Tasks 7-10: DiarizationAdapter + current_speaker_id    │
│     in PolicyInputs + diarized speaker continuity tie-breaker  │
│   Phase A.5 Task 0c (v0.2 roadmap Wave 0′): SyntheticClock +   │
│     DirectAudioInputFeeder + FixtureScenarioDriver +           │
│     test_eval_case_replay_bit_identical un-skipped             │
│   Phase B2: FailureSliceExtractor concrete (live examiner      │
│     reuses the causal-subgraph machinery for per-utterance     │
│     failure diagnosis)                                         │
└──────────────┬─────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────┐
│ Wave 1 — Framework + fixture pack (parallel-safe)             │
│   T1: LiveExaminerCaseSource (Protocol impl, synthetic-mode    │
│        default, real-mode NotImplementedError)                 │
│   T2: Curated ≥3 multi-speaker fixture pack                    │
│        (tests/fixtures/phase_c/)                               │
└──────────────┬─────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────┐
│ Wave 2 — Metrics                                               │
│   T3: Speaker-attributed metric implementations                │
│        (companion_harness/evals/metrics/speaker_attributed.py) │
└──────────────┬─────────────────────────────────────────────────┘
               │
               ▼
┌────────────────────────────────────────────────────────────────┐
│ Wave 3 — Contract test + doc reconciliation                    │
│   T4: test_phase_c_runs_on_real_diarized_session_end_to_end   │
│   T5: Update roadmap-eval-draft.md Phase C section             │
│        (v0.2-shipped vs v0.3-deferred scope)                   │
└────────────────────────────────────────────────────────────────┘
```

---

## §Concurrency map

| Wave | Concurrency | Tasks | Notes |
|---|---|---|---|
| External-gates | sequential gate | v0.2b T7-10 + Phase A.5 + Phase B2 | NO Wave 1 dispatch until v0.2b is merged (`current_speaker_id` in `PolicyInputs`). Phase A.5 + B2 are independent v0.2 work; coordinate via the project-progress doc. |
| 1 | **2×** | T1, T2 | T1 (code) and T2 (fixture curation) touch disjoint surfaces. T1 needs the fixture-pack shape spec (in T2's PR description) but can stub against a synthetic 3-speaker JSONL until T2 lands the real recordings. |
| 2 | **1×** | T3 | Metrics depend on T1 (case shape) + T2 (ground-truth label format). |
| 3 | **2×** | T4, T5 | T4 is the end-to-end contract test (depends on T1+T2+T3). T5 is a docs-only update to roadmap-eval-draft.md and can ship anytime after T1 lands. |

**Max realistic concurrency: 2 agents** (Wave 1 fan-out). Wave 1 → Wave 2 is sequential by data-shape dependency; Wave 2 → Wave 3 is sequential by test-target dependency.

---

## §Foundation-first ordering

**External gates are blocking.** v0.2d cannot dispatch coders until:
- v0.2b's `DiarizationAdapter` Protocol + `PyannoteDiarizationAdapter` + `current_speaker_id: str | None` field on `PolicyInputs` are merged (v0.2 roadmap Wave 2 Tasks 7-9). The fixture pack's ground-truth speaker labels are the validation surface for the diarization adapter; without the adapter, T4's contract test has nothing to assert against.
- Phase A.5's `FixtureScenarioDriver` and `test_eval_case_replay_bit_identical` are green (v0.2 roadmap Wave 0′ / Task 0c). v0.2d reuses the FixtureScenarioDriver to inject recorded audio into the orchestrator under SyntheticClock.
- Phase B2's `FailureSliceExtractor` concrete is merged (roadmap-eval-draft.md Phase B2). T3's per-utterance metrics surface per-case failure slices when a case fails its ground-truth match; this requires the upstream extractor.

If any of the three gates slip, v0.2d stays in plan-only state. **No partial framework lands without the gates** — the framework's only value is in being end-to-end exercisable on a real diarized session.

---

## §Open questions (resolved as leans where possible)

### OQ-1 — Recorded-session fixture format

**Lean:** Per-session directory containing:
```
tests/fixtures/phase_c/<session_id>/
  event_log.jsonl          # replay-safe event log (per spec invariant #10 + privacy policy)
  audio.wav                # mono, 16 kHz, source audio
  ground_truth_speakers.json   # per-utterance: {utterance_id, t_start_ms, t_end_ms, speaker_id, addressed_agent: bool}
  manifest.json            # session_id, recording_date, speaker_count, duration_ms, license, redaction_notes
```

The event log is the canonical timing/causality artifact. `audio.wav` is the playback artifact for the live diarization adapter under FixtureScenarioDriver's `DirectAudioInputFeeder`. `ground_truth_speakers.json` is the labelling-team output that the speaker-attributed metric compares against.

**Justification:** mirrors the per-case directory layout already used in `companion_harness/fixtures/policy_replay_001/` (case.json + assets), extends with ground-truth labels separated from the event log so the labels are not confused with replay-derived data.

**Alternative considered:** single JSON-with-base64-audio blob. Rejected — blocks `git diff` review of the event log and bloats the repo.

### OQ-2 — Where fixtures live

**Lean:** `tests/fixtures/phase_c/` (per eval-subsystem-spec layout pattern; sibling of any future `tests/fixtures/phase_b2/`). The directory contains one subdirectory per session. `tests/fixtures/phase_c/README.md` documents the labelling convention, the per-session manifest schema, and the recording-consent / license posture.

**Justification:** matches `companion_harness/evals/scenarios/fixture.py` expectations for fixture-tree shape; keeps Phase C fixtures separable from Phase A.5 / B2 fixtures so a single benchmark adapter's fixture corpus is easy to enumerate.

**Risk:** repo bloat. Mitigation: cap fixture-pack total size at ~50 MB; 3 short (≤30 s) multi-speaker clips is enough to exercise the framework end-to-end. Full-scale corpora live off-tree and are referenced by hash in v0.3.

### OQ-3 — What metrics Phase C produces vs Phase A.5

**Lean — Phase A.5 produces** (per fixture):
- `policy_replay_bit_identical` (boolean, per fixture, per pair of runs).
- `event_log_orphan_count` (per fixture).

**Lean — Phase C produces** (per session, per speaker):
- `addressing_accuracy_per_speaker` — fraction of utterances whose harness-derived `current_speaker_id` matches the ground-truth label. Computed per speaker; aggregate is unweighted mean.
- `turn_gap_ms_distribution_per_speaker` — histogram of inter-utterance gaps split by ground-truth speaker.
- `replay_match_rate_vs_ground_truth` (per session) — fraction of utterances where the harness's `SpeakDecision` against the diarized session matches the ground-truth expected addressing decision. This is the headline Phase C metric.

**Justification:** Phase A.5 metrics validate the **fixture machinery** (replay is deterministic, the event-log DAG closes). Phase C metrics validate the **diarization-aware policy path** end-to-end against human-labelled ground truth. Distinct surfaces, distinct metric names, no overlap.

### OQ-4 — Recording-consent posture for the fixture pack

**Lean:** Three options, in preference order:
1. Synthesized multi-speaker audio (combine single-speaker public-domain TTS samples with deterministic timing). Pro: zero consent risk. Con: not "real" diarization input.
2. Public-domain or CC-BY multi-speaker recordings (LibriVox dialogues, Mozilla Common Voice paired clips). Pro: real audio; license-clean. Con: not conversational.
3. Operator-recorded sessions with documented consent + redaction. Pro: realistic. Con: requires consent process.

**Lean for v0.2:** Option 1 for at least 1 fixture (`phase_c_synthetic_two_speaker_001`); option 2 for at least 1 fixture (`phase_c_libripaired_001`); option 3 deferred unless operator records a session and the consent doc is checked in. **All three options together satisfy "≥3 multi-speaker recordings."** This is OPEN — the labelling-team / project-lead decides per-fixture posture.

### OQ-5 — Examiner Protocol (is it populated for v0.2d?)

**Lean: NO.** `BenchmarkAdapter.examiner` stays `None` for v0.2d's Phase C adapter. The "live examiner" name in roadmap-eval-draft.md describes a future LLM-driven partner; in v0.2d's recorded-session mode there is no live partner — the audio is pre-recorded. The Examiner Protocol slot stays available for the v0.3 expansion to LLM-driven partner play.

**Justification:** populating `Examiner` for a recorded session would require synthesizing a `respond()` that replays recorded turns, which is a degenerate shape. Cleaner to leave the slot `None` and document that the v0.3 expansion adds the live partner.

### OQ-6 — Does v0.2d touch POLICY_VERSION?

**Lean: NO.** POLICY_VERSION moves to `v0.1k` at v0.2b's Wave 2 Task 9 (diarization tie-breaker). v0.2d adds NO new policy rule; it only consumes the `current_speaker_id` field. The final `v0.2-final` bump is v0.2 roadmap Wave 6 Task 20.

---

## §10 Cross-milestone coordination

- **v0.2b (DiarizationAdapter + PolicyInputs.current_speaker_id, Wave 2 Tasks 7-10) MUST merge before v0.2d Wave 1 dispatch.** The dispatcher MUST verify the v0.2b merge commit before opening any T1/T2 PR. If v0.2b slips, v0.2d stays in plan-only state.
- **Phase A.5 Task 0c (FixtureScenarioDriver replay-safe contract) MUST be green** before T4. T4 piggybacks on the FixtureScenarioDriver for audio injection.
- **Phase B2 FailureSliceExtractor concrete MUST be merged** before T3 ships, OR T3 ships without per-utterance failure slices (metrics-only) and a follow-on PR adds slice integration. **Lean: gate T3 on B2** to avoid two-pass landing.
- **v0.3 deferred surface (LLM-driven live partner, Full-Duplex-Bench v2)** is documented in T5's roadmap-eval-draft.md update so future planners know what was intentionally NOT shipped in v0.2.

---

# TASKS

---

## Task T1 — `LiveExaminerCaseSource` (framework, synthetic-mode default)

**Files touched**
- `companion_harness/evals/adapters/live_examiner.py` — NEW. Implements `CaseSource` Protocol per `companion_harness/evals/protocols.py:20-25`.
- `companion_harness/evals/adapters/__init__.py` — export `LiveExaminerCaseSource` (surgical addition; do not rewrite the file).

**Implementation sketch**
1. Module docstring: explicitly state synthetic-mode default + real-mode `NotImplementedError` + v0.3 pointer (mirror the structure of `companion_harness/evals/adapters/candor.py` lines 1-26).
2. `class LiveExaminerCaseSource:`
   - Class attrs: `name = "live_examiner_phase_c"`, `version = "0.2"`.
   - `__init__(self, fixtures_root: Path, mode: str = "synthetic")`: store `fixtures_root` (default `Path("tests/fixtures/phase_c")`); validate `mode in {"synthetic", "real"}`; if `mode == "real"`, raise `NotImplementedError("Phase C real-mode (live LLM-driven partner) deferred to v0.3; see docs/roadmap-eval-draft.md Phase C v0.3-deferred section")`.
3. `def iter_cases(self, split: str) -> Iterable[EvaluationCase]:`
   - List subdirectories of `fixtures_root` (the per-session dirs from OQ-1).
   - For each session subdir, read `manifest.json` + `ground_truth_speakers.json`.
   - For each utterance in the ground-truth file, yield one `EvaluationCase` with:
     - `case_id = f"{session_id}_{utterance_id}"`
     - `fixture_ref = str(session_subdir)`
     - `expected_outcomes = {"addressed_agent": utterance["addressed_agent"], "speaker_id": utterance["speaker_id"]}`
     - `policy_version` sourced from the v0.2b-introduced `POLICY_VERSION` constant (do NOT hardcode a string literal — coding rule 3).
   - `split` parameter respected if the manifest declares a split partition; otherwise treat as a no-op and yield all utterances (synthetic-mode fixture packs are small enough to not need splits).
4. Surface area is minimal: no examiner instantiation, no LLM call, no live audio synthesis. The recorded fixture IS the case data.
5. NO model SDK imports (eval-subsystem-spec.md Anchor 1 — import direction). Pure stdlib + `companion_harness.schemas`.

**Test plan**
- New unit test `tests/test_live_examiner_case_source.py`:
  - Instantiate with `mode="synthetic"` against a temp dir containing 1 synthetic session subdir; assert `iter_cases("test")` yields the expected number of `EvaluationCase` instances with correct `expected_outcomes`.
  - Instantiate with `mode="real"`; assert `NotImplementedError` raised.
  - Run under canonical venv: `/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_live_examiner_case_source.py -q`.
- Import-direction contract: `tests/test_runtime_does_not_import_evals` already enforces Anchor 1; running the full suite after T1 lands must keep it green.

**Success criterion**
```
/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_live_examiner_case_source.py -q  # all green
/raid/yid042/venvs/companion-harness/bin/python3 -c "from companion_harness.evals.adapters.live_examiner import LiveExaminerCaseSource; assert LiveExaminerCaseSource.name == 'live_examiner_phase_c'"
```

**Anchors locked** — eval-subsystem-spec.md Anchor 5 (six small protocols); v0.2 roadmap pinned-success-criterion #3 (framework shipped).

**OQs (pre-resolved leans)** — OQ-1, OQ-5, OQ-6 above.

**Cross-references** — `companion_harness/evals/protocols.py:20-25` (`CaseSource` Protocol); `companion_harness/evals/adapters/candor.py` (synthetic-mode pattern); `docs/eval-subsystem-spec.md` Anchor 1 + 5.

**Blocker dependencies** — v0.2b Tasks 7-9 merged (POLICY_VERSION constant for v0.1k present; `current_speaker_id` field on `PolicyInputs`); v0.2 roadmap Wave 0′ Task 0c green (FixtureScenarioDriver).

---

## Task T2 — Curated multi-speaker fixture pack (≥3 sessions)

**Files touched**
- `tests/fixtures/phase_c/README.md` — NEW. Documents per-session directory shape, labelling convention, license posture per OQ-4, redaction policy.
- `tests/fixtures/phase_c/<session_id>/manifest.json` — NEW per session.
- `tests/fixtures/phase_c/<session_id>/event_log.jsonl` — NEW per session. Replay-safe (per `docs/eval-subsystem-spec.md` privacy section).
- `tests/fixtures/phase_c/<session_id>/audio.wav` — NEW per session (mono, 16 kHz; total fixture-pack audio ≤ ~50 MB per OQ-2 cap).
- `tests/fixtures/phase_c/<session_id>/ground_truth_speakers.json` — NEW per session.

**Implementation sketch**
1. Per OQ-4 lean, curate ≥3 sessions across the option-1/option-2/option-3 mix. Minimum acceptable set:
   - `phase_c_synthetic_two_speaker_001` — option-1 (synthesized; TTS-stitched).
   - `phase_c_synthetic_three_speaker_001` — option-1 (synthesized; 3 speakers).
   - `phase_c_libripaired_two_speaker_001` — option-2 (public-domain CC-BY).
   (Option-3 deferred unless an operator-recorded session is ready.)
2. For each session:
   - Record / synthesize raw audio; trim to ≤30 s; resample to 16 kHz mono.
   - Run a fresh harness session under FixtureScenarioDriver against the audio; capture the event log (replay-safe — no raw audio bytes in the log; reference by hash to the `audio.wav` file).
   - Manually label each utterance with speaker_id + addressed_agent boolean; emit `ground_truth_speakers.json`.
   - Write `manifest.json` per the OQ-1 schema.
3. Add a fixture-integrity contract test scaffold (placeholder; full enforcement in T4):
   - Per-session checks (added to T4): manifest fields present, event log JSONL parses, audio file's hash matches the manifest reference, ground_truth labels cover the full duration.
4. License posture: `README.md` documents each session's license + recording-consent posture (none for option-1, CC-BY for option-2, consent doc cited for option-3 if added).
5. Repo bloat budget: `du -sh tests/fixtures/phase_c/` ≤ 60 MB. If a session's audio pushes the budget over, downsample further or trim.

**Test plan**
- The fixture-pack PR's own success criterion is structural (files exist, schemas validate). End-to-end testing is T4.
- Add `tests/test_phase_c_fixture_manifest_valid.py`: iterate `tests/fixtures/phase_c/*/manifest.json`, assert required keys present (`session_id`, `recording_date`, `speaker_count`, `duration_ms`, `license`, `redaction_notes`).

**Success criterion**
```
ls tests/fixtures/phase_c/ | grep -E "phase_c_" | wc -l  # ≥ 3
du -sh tests/fixtures/phase_c/  # ≤ 60M
/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_phase_c_fixture_manifest_valid.py -q  # green
```

**Anchors locked** — OQ-1, OQ-2, OQ-4 leans above.

**Cross-references** — `companion_harness/fixtures/policy_replay_001/` (per-case dir pattern); `docs/eval-subsystem-spec.md` §Determinism + privacy.

**Blocker dependencies** — v0.2b Task 8 (`PyannoteDiarizationAdapter` available so the event log can be regenerated with real diarization output if needed); v0.2 roadmap Wave 0′ Task 0c (FixtureScenarioDriver).

---

## Task T3 — Speaker-attributed metric implementations

**Files touched**
- `companion_harness/evals/metrics/speaker_attributed.py` — NEW.
- `companion_harness/evals/metrics/__init__.py` — surgical export addition; do not rewrite.

**Implementation sketch**
1. Module docstring: state the three metrics this module implements (OQ-3 lean), their input shape (a `ReplayRun` whose event log contains diarization output + a paired `ground_truth_speakers.json` accessible via `case.fixture_ref`), and their output shape (`MetricValue` per `companion_harness/evals/schemas.py`).
2. Three concrete metric classes implementing the `Metric` Protocol (`companion_harness/evals/protocols.py:37-41`):
   - `class AddressingAccuracyPerSpeakerMetric:` — `name = "addressing_accuracy_per_speaker"`. `compute(replay_run)` reads the replay's event log for `addressing_classified` events, matches each event's `current_speaker_id` against the ground-truth label by utterance_id, returns a per-speaker dict + aggregate unweighted mean as a `MetricValue` with `breakdown` payload.
   - `class TurnGapMsDistributionPerSpeakerMetric:` — `name = "turn_gap_ms_distribution_per_speaker"`. Computes per-speaker inter-utterance gap histograms from the event log timing, returns histogram bins as `breakdown`.
   - `class ReplayMatchRateVsGroundTruthMetric:` — `name = "replay_match_rate_vs_ground_truth"`. For each utterance, compare the harness's recorded `SpeakDecision.action_class` against the ground-truth `addressed_agent` expectation (addressed → `SPEAK`; not-addressed → `STAY_SILENT`). Returns fraction matched as the headline scalar.
3. NO model SDK imports. Pure stdlib + `companion_harness.schemas` + `companion_harness.evals.schemas`.
4. Metric input access pattern: the metrics read `ground_truth_speakers.json` via `replay_run.case.fixture_ref / "ground_truth_speakers.json"`. This is the same pattern Phase A.5's `FixtureScenarioDriver` uses for fixture-asset access — no new I/O abstraction.
5. Failure-slice integration: when `ReplayMatchRateVsGroundTruthMetric` returns < 1.0, the per-utterance mismatches are surfaced via the standard `FailureSliceExtractor` (Phase B2 surface). T3 does NOT reimplement slice extraction; it just emits structured per-utterance mismatch records that the extractor can consume.

**Test plan**
- `tests/test_phase_c_metrics_speaker_attributed.py`: synthetic `ReplayRun` (constructed in-test, no real diarization needed) + synthetic `ground_truth_speakers.json`; assert each of the three metrics computes the expected `MetricValue`.
- Run under canonical venv.

**Success criterion**
```
/raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_phase_c_metrics_speaker_attributed.py -q  # green
/raid/yid042/venvs/companion-harness/bin/python3 -c "from companion_harness.evals.metrics.speaker_attributed import AddressingAccuracyPerSpeakerMetric, TurnGapMsDistributionPerSpeakerMetric, ReplayMatchRateVsGroundTruthMetric"
```

**Anchors locked** — OQ-3 lean (distinct from Phase A.5 metric surface).

**OQs (pre-resolved leans)** — OQ-3.

**Cross-references** — `companion_harness/evals/protocols.py:37-41` (`Metric` Protocol); `companion_harness/evals/metrics/distributional.py` (existing per-metric class pattern); `companion_harness/evals/schemas.py` (`MetricValue` shape).

**Blocker dependencies** — T1 merged (case shape); T2 merged (ground-truth label schema); Phase B2 FailureSliceExtractor concrete (for slice integration; if B2 slips, T3 ships metrics-only and a follow-on PR wires slices).

---

## Task T4 — End-to-end contract test on a real diarized session

**Files touched**
- `tests/test_phase_c_runs_end_to_end_on_real_diarized_session.py` — NEW.

**Implementation sketch**
1. The test target: a single end-to-end run of the Phase C adapter against `phase_c_libripaired_two_speaker_001` (or whichever T2 fixture is the canonical "real" one), under canonical venv, asserts:
   - `LiveExaminerCaseSource(mode="synthetic").iter_cases("test")` yields N cases for the chosen session, where N matches the session's `ground_truth_speakers.json` utterance count.
   - For each case, the `FixtureScenarioDriver.run(case, ...)` produces a `ReplayRun` whose event log:
     - parses as valid JSONL,
     - contains at least one `addressing_classified` event with a non-null `current_speaker_id`,
     - has zero orphan events (every `Event.caused_by[]` resolves within the log).
   - The three T3 metrics (`AddressingAccuracyPerSpeakerMetric`, `TurnGapMsDistributionPerSpeakerMetric`, `ReplayMatchRateVsGroundTruthMetric`) all compute without error against the produced `ReplayRun`.
   - The aggregate `ReplayMatchRateVsGroundTruthMetric` value is ≥ 0.7 (numeric gate per §Numeric gates below). This is the headline pass condition for Phase C in v0.2.
2. Test marker: `@pytest.mark.requires_diarization` (custom marker; skipped if v0.2b's diarization isn't available — but at land time it MUST run, so the skip-reason is purely an environment guard).
3. The test exercises the FULL stack: `LiveExaminerCaseSource` → `FixtureScenarioDriver` → orchestrator under `SyntheticClock` → `DiarizationAdapter.process_chunk()` → `PolicyInputs.current_speaker_id` → `SpeakPolicy.decide()` → recorded event log → T3 metrics. If any layer breaks, the test fails at the relevant assertion boundary.
4. NO mocks of v0.2b adapters. NO mocks of the orchestrator. NO test-only stubs. If a real component is missing at test time, the test SKIPs with an explicit reason (`requires_diarization` marker) — it does NOT fake it.

**Test plan**
- Local under canonical venv:
  ```
  /raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_phase_c_runs_end_to_end_on_real_diarized_session.py -q -v
  ```
- Remote (b200) if diarization model weights aren't on local:
  ```
  ssh b200 'cd ~/companion-agent-harness && /raid/yid042/venvs/companion-harness/bin/python3 -m pytest tests/test_phase_c_runs_end_to_end_on_real_diarized_session.py -q -v'
  ```

**Success criterion**
```
pytest tests/test_phase_c_runs_end_to_end_on_real_diarized_session.py -q  # green under canonical venv with diarization enabled
```

**Anchors locked** — v0.2 roadmap pinned-success-criterion #3 (Phase C runs end-to-end against the fixture pack).

**Cross-references** — `companion_harness/evals/scenarios/fixture.py` (`FixtureScenarioDriver`); v0.2b's `DiarizationAdapter` surface; `companion_harness/replay.py` (when `run_tier_b_replay` becomes available — currently Phase A.5+1; the test does NOT require it).

**Blocker dependencies** — T1, T2, T3 all merged; v0.2b Wave 2 Tasks 7-10 merged + `--enable-diarization` defaults documented; Phase A.5 Task 0c green.

---

## Task T5 — Update `roadmap-eval-draft.md` Phase C section

**Files touched**
- `docs/roadmap-eval-draft.md` — surgical edit of the Phase C section (lines 233-263). NOT a rewrite.

**Implementation sketch**
1. Read the existing Phase C section in `docs/roadmap-eval-draft.md` (lines 233-263 of the file).
2. Restructure the section into three sub-sections:
   - **v0.2-shipped scope (Phase C framework + small fixture pack)** — restate T1/T2/T3/T4 success criteria; explicitly name `LiveExaminerCaseSource` and the `tests/fixtures/phase_c/` directory.
   - **v0.3-deferred scope (live LLM-driven partner + Full-Duplex-Bench v2)** — restate the original Phase C envelope (LLM examiner side-process; `fixture_audio_chunk_injected` produced live; FDB v2 case-source); state that v0.2 intentionally did NOT ship this.
   - **Gating prerequisites (unchanged from original)** — preserve the existing prerequisite list; add a note that v0.2d satisfies the addressing + diarization prerequisites via v0.2b.
3. Preserve the original Phase C tasks T-C1/T-C2/T-C3 as bullet entries under the v0.3-deferred sub-section (do NOT delete — they remain valid forward-looking work items).
4. Add cross-references to `docs/plan-v0.2d-execution.md` (this plan) and `docs/roadmap-v0.2-draft.md` (Wave 4).
5. Surgical edits only — do not reformat or reflow adjacent sections.

**Test plan**
- Visual review: the rendered Markdown still reads cleanly; no broken cross-references.
- `git diff docs/roadmap-eval-draft.md` should be confined to the Phase C section (lines 233-263 ± a few cross-reference additions).
- No code or test changes; documentation-only PR.

**Success criterion**
```
git diff --stat docs/roadmap-eval-draft.md  # only one file touched
grep -c "v0.2-shipped scope" docs/roadmap-eval-draft.md  # ≥ 1
grep -c "v0.3-deferred scope" docs/roadmap-eval-draft.md  # ≥ 1
grep -c "plan-v0.2d-execution" docs/roadmap-eval-draft.md  # ≥ 1
```

**Anchors locked** — none new; this task restates v0.2 roadmap's pinned-success-criterion #3 into the eval-roadmap surface.

**Cross-references** — `docs/roadmap-v0.2-draft.md` Wave 4; `docs/plan-v0.2d-execution.md` (self-link).

**Blocker dependencies** — T1 merged (so the doc can reference the shipped `LiveExaminerCaseSource` by name).

---

## §Numeric gates

> Each gate carries `owner`, `measurement_method`, `sample_size`, and `gate_type` per v0.2 roadmap convention.

| Metric | Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|---|
| `phase_c_fixture_session_count` | ≥ 3 | T2 owner | `ls tests/fixtures/phase_c/ \| grep -E 'phase_c_' \| wc -l` | One sample at T2 merge | blocking |
| `phase_c_fixture_pack_total_size_mb` | ≤ 60 | T2 owner | `du -sm tests/fixtures/phase_c/` | One sample at T2 merge | blocking |
| `phase_c_fixture_manifest_validity_rate` | == 1.0 | T2 owner | `tests/test_phase_c_fixture_manifest_valid.py` pass rate | All fixtures in pack | blocking |
| `phase_c_metric_unit_test_pass_rate` | == 1.0 | T3 owner | `tests/test_phase_c_metrics_speaker_attributed.py` pass rate | All 3 metric classes | blocking |
| `phase_c_end_to_end_test_pass_rate` | == 1.0 | T4 owner | `tests/test_phase_c_runs_end_to_end_on_real_diarized_session.py` pass rate, canonical venv, diarization enabled | All test invocations in CI / pre-merge | blocking |
| `phase_c_replay_match_rate_vs_ground_truth` | ≥ 0.7 (headline) | T4 owner | `ReplayMatchRateVsGroundTruthMetric` aggregate on the canonical T2 fixture (`phase_c_libripaired_two_speaker_001`) | All utterances in that fixture | blocking |
| `phase_c_addressing_accuracy_per_speaker_aggregate` | ≥ 0.7 (informational baseline; tightened in v0.3) | T4 owner | `AddressingAccuracyPerSpeakerMetric` unweighted mean on canonical fixture | All utterances in that fixture | informational |
| `phase_c_event_log_orphan_count` | == 0 | T4 owner | Walk every `Event` in the produced `ReplayRun`'s event log; count `caused_by[]` entries that don't resolve to an in-log `event_id` | Per session | blocking |
| `phase_c_real_mode_raises_not_implemented_error` | == True | T1 owner | `tests/test_live_examiner_case_source.py::test_real_mode_raises` | Single assertion | blocking |
| `phase_c_runtime_import_isolation` | == True | T1 owner | `tests/test_runtime_does_not_import_evals` stays green after T1 lands | Per pytest run | blocking |
| `phase_c_local_ci_pass_rate` | == 1.0 | Each task owner | `pytest` pass rate on local contract suite | All contract tests | blocking |
| `phase_c_remote_smoke_pass_rate` | == 1.0 | Each task owner | `pytest` pass rate on b200 GPU smoke suite (diarization-dependent tests run here) | All b200-tagged smoke tests | blocking |

**Headline gate** — `phase_c_replay_match_rate_vs_ground_truth ≥ 0.7`. v0.2 ships an honest baseline; v0.3 tightens to ≥ 0.85 once the LLM-driven live-partner mode is in play. The 0.7 threshold is set deliberately low to avoid forcing fixture-curation gymnastics in v0.2; the headline matters at v0.3.

**Note on `phase_c_remote_smoke_pass_rate`** — T4's end-to-end test requires diarization model weights. If those weights are b200-only (per the canonical venv discipline at `docs/remote-dev.md`), T4 runs on b200, not on the local machine. Coordinate with the b200 smoke-pipeline owner.

---

## §Fixture manifest (additive to v0.2 roadmap)

- `phase_c_synthetic_two_speaker_001` — option-1 (synthesized TTS-stitched), 2 speakers, ≤30 s.
- `phase_c_synthetic_three_speaker_001` — option-1, 3 speakers, ≤30 s.
- `phase_c_libripaired_two_speaker_001` — option-2 (public-domain CC-BY pairing), 2 speakers, ≤30 s. **This is the canonical fixture for T4's headline metric.**

(Operator-recorded session — option-3 — is deferred unless an operator records and consent-doc-s a session during v0.2d execution.)

These add to v0.2 roadmap's existing fixture-manifest line for `live_examiner_diarized_session_001`; that line is satisfied by the `phase_c_libripaired_two_speaker_001` fixture per the v0.2-shipped-scope decision.

---

## §Coordination notes

- **v0.2b merge gate.** v0.2d's dispatcher (whether human or `parallel-developing` skill) MUST verify `git log --oneline | grep -E 'diariz.*PolicyInputs|current_speaker_id'` shows v0.2b's Wave 2 merge before opening any T1/T2 PR. No partial v0.2d landing on a v0.2b stub.
- **Phase A.5 dependency.** If Phase A.5's `FixtureScenarioDriver` isn't green (i.e., `test_eval_run_replay_safe` still xfailed), T4 cannot land. Coordinate with the Phase A.5 owner.
- **Phase B2 dependency.** If Phase B2's `FailureSliceExtractor` isn't merged, T3 ships metrics-only with a TODO; a follow-on PR adds slice wiring. Document the deferral in T3's PR description.
- **POLICY_VERSION.** v0.2d does NOT bump POLICY_VERSION. The v0.1k bump comes from v0.2b; the v0.2-final bump is v0.2 roadmap Wave 6 Task 20.
- **Fixture curation labour.** Manual labelling of `ground_truth_speakers.json` is the labour-bottleneck of v0.2d. Budget: ≤ 2 hours per session at the ≤30 s clip length. Three sessions = ≤ 6 hours of labelling. If labelling exceeds budget, reduce per-session clip length further.
- **License auditing.** Option-2 fixtures (LibriVox / Mozilla Common Voice) require per-clip license citation in `README.md`. Project-lead reviews before T2 merges.
- **No CI runtime regressions.** T4's end-to-end test adds non-trivial wall-clock to the canonical-venv smoke run (orchestrator boot + diarization load + per-utterance processing for 3 sessions). If the runtime impact exceeds 60 s, mark the test `@pytest.mark.slow` and run it only on b200 smoke.

---

## §Cross-references

- Spec: `docs/architecture-v0.1.md` (FROZEN — never edit). Part 2 invariants #1 (no unlogged behavior), #5 (deterministic policy replay), #6 (Tier-A behavioral tolerance), #10 (EventLogger async/non-blocking) all hold under Phase C.
- Eval subsystem spec: `docs/eval-subsystem-spec.md` (Anchor 1 import direction; Anchor 5 six small protocols; §Determinism + privacy).
- v0.2 milestone roadmap: `docs/roadmap-v0.2-draft.md` (Wave 4 Tasks 15a/15b; pinned-success-criterion #3).
- Eval-subsystem roadmap: `docs/roadmap-eval-draft.md` (Phase C — updated by T5).
- Sibling v0.2 plans (in-flight): `docs/plan-v0.2a-execution.md` (BackgroundReasoner), `docs/plan-v0.2b-execution.md` (diarization — **dependency**), `docs/plan-v0.2c-execution.md` (benchmark loaders).
- Phase A.5 plan-of-record: `docs/plan-eval-phase-a-execution.md` (precedes Phase A.5; Phase A.5 is roadmap-eval-draft.md §Phase A.5).
- Replay surface: `companion_harness/replay.py` (currently 7 lines of docstring; `run_tier_b_replay` is Phase A.5+1 — v0.2d does NOT depend on its implementation).
- Fixture pattern reference: `companion_harness/fixtures/policy_replay_001/case.json` (per-case dir shape).

---

## §Risks

1. **v0.2b slip.** If v0.2b's diarization adapter slips past v0.2d's intended dispatch window, v0.2d stays in plan-only state. Mitigation: explicit dependency gate at the top of §Dependency graph; dispatcher verifies before opening PRs.
2. **Fixture-pack labelling fatigue.** Manual ground-truth labelling of 3 multi-speaker sessions is labour-intensive. Mitigation: short clip lengths (≤30 s); use synthesized audio for 2/3 fixtures to bound the manual labour to one option-2 clip.
3. **`replay_match_rate_vs_ground_truth` < 0.7 on canonical fixture.** If v0.2b's diarization adapter's real-world addressing accuracy is below the 0.7 headline gate, T4 fails. Mitigation: the 0.7 threshold is deliberately conservative; if it fails, root-cause to either the fixture (relabel) or the diarization adapter (file v0.2b follow-up); do NOT lower the gate silently.
4. **License audit ambiguity for option-2 fixtures.** CC-BY clips have attribution requirements that must propagate to the eval reports. Mitigation: T2's `README.md` enumerates per-fixture attribution strings; future eval reports include the strings in the report header.
5. **v0.3 scope confusion.** Future planners may read "Phase C shipped in v0.2" and assume the full LLM-driven live-partner mode is shipped. Mitigation: T5's roadmap-eval-draft.md update explicitly partitions v0.2-shipped from v0.3-deferred scope; both `LiveExaminerCaseSource`'s module docstring and the `real-mode raises NotImplementedError` path cite the v0.3 deferral by docs path.
6. **b200 runtime cost for T4.** End-to-end test against 3 sessions on b200 may exceed the smoke-suite budget. Mitigation: `@pytest.mark.slow` marker + nightly run rather than per-PR run; document in `docs/remote-dev.md`.

---

## §Out of scope (deferred to v0.3+)

- LLM-driven side-process examiner producing live utterances (`fixture_audio_chunk_injected` produced from a live partner).
- Full-Duplex-Bench v2 case-source and scoring.
- Self-play (one harness as examiner of another).
- Cross-session speaker recognition (single-session at v0.2).
- Tier-B replay verification of Phase C runs (depends on `run_tier_b_replay` which is Phase A.5+1 deferred).
- Automatic ground-truth labelling (every v0.2d fixture is hand-labelled).
- Tighter `replay_match_rate_vs_ground_truth` gate (0.7 in v0.2; tightened to ≥ 0.85 in v0.3 alongside the live-partner mode).
- Phase C dashboard surface (eval-specific dashboards are out of scope per `docs/eval-subsystem-spec.md` §Out of scope).

---

**End of draft.** Sign-off pending plan-critic round + project-lead review.
