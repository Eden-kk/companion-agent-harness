# Project progress — 2026-05-15

Session-handoff snapshot. Read this first if you're picking up the project cold and helping the operator do a manual test.

If you want to *run a session*: read [`manual-test-handbook.md`](manual-test-handbook.md).
If you want to *understand the adapter inventory*: read [`model-stack.md`](model-stack.md).
If you want the *frozen spec*: read [`architecture-v0.1.md`](architecture-v0.1.md).

---

## Where we are

**All v0.1 milestones (a → j) are shipped and tagged.** Plus Eval Phases A, A.5, B1, B2, D in flight; Tier-B replayer + dashboard + 4-store memory + tool routing + aesthetic rubric + attachment-risk monitor all merged on `main`.

| Milestone | Status | Scope |
|---|---|---|
| v0.1a | ✅ shipped | Stage 0 + minimal Stage 1 — event schema, EventLogger, basic ASR/VAD adapters |
| v0.1b | ✅ shipped | Backchannel-aware EOU (SmartTurn v3 + backchannel classifier) |
| v0.1c | ✅ shipped | Stage 2 audio-video grounding (VisionSidecar Protocol seams + stubs) |
| v0.1d | ✅ shipped | Stage 3 speak/silence policy + live-loop integration |
| v0.1e | ✅ shipped | Stage 4 four-store memory + sleep-time agent |
| v0.1f | ✅ shipped | Stage 5 background reasoning + tool routing (fast path + barge-in + filler budget) |
| v0.1g | ✅ shipped | Stage 6 companion texture (aesthetic rubric + attachment risk + recent shared moments) |
| v0.1h | ✅ shipped | Vision sidecar wiring + memory live-pipeline wiring |
| v0.1i | ✅ shipped | Dashboard + ConfigStore + HTTP `/config/*` endpoints |
| v0.1j | ✅ shipped | Real producers for signal seams (native_duplex EOU, MiniCPM addressing) + ASR transcript through retrieval + TTS adapter selector |

**Eval subsystem** (`companion_harness/evals/`):

| Phase | Status | Adapters |
|---|---|---|
| A | ✅ | `harness_native` (wraps 4 contract tests); CLI; JSON + MD reporters |
| A.5 | ✅ | SyntheticClock + DirectAudioInputFeeder + FixtureScenarioDriver |
| B1 | ✅ | `CandorCaseSource` (synthetic-mode; real CANDOR data deferred) |
| B2 | ✅ | `FullDuplexBenchV1/V1.5CaseSource` (synthetic-mode) |
| C | 📋 deferred | Live examiner — gated on real diarization |
| D | 🟡 in flight | VocalBench (merged #232); VoiceBench (#233); HumDial-FDBench (PR pending) |

---

## What the operator can do right now

Run the manual-test console:

```bash
/raid/yid042/venvs/companion-harness/bin/python3 \
    -m manual_test_console.server --host 0.0.0.0 --port 8800 \
    --blob-dir /tmp/manual_test_blobs
```

Then `http://localhost:8800/` in a browser. Cold MiniCPM-o load: ~60–120 s.

**End-to-end working paths** (verified post-v0.1j):
1. Speak → Silero VAD → SmartTurn v3 → faster-whisper-tiny ASR → MiniCPM addressing classifier → 4-store memory retrieve → SpeakPolicy v0.1j → MiniCPM-o proposal → Kokoro TTS → browser playback.
2. Tier-B threshold tuning via right-column dashboard (12 knobs; live patch; audit-event-emitting).
3. Tool call → FastToolDispatcher → ToolProgressEmitter (filler-budget state machine) → 300 ms barge-in cancellation.
4. Privacy-gated memory commits (`SKIPPED_PRIVACY` / `SKIPPED_CAMERA`).
5. `forget that` explicit-forget detection.
6. Decision-trace click-through (every `policy_decision` row → `PolicyInputs` + gates blob).
7. Vision sidecar with `--enable-vision` (frames reach MiniCPM-o, but scoring/grounding are still stub-zero).

---

## What's still stubbed

8 Protocol seams return neutral values. Real adapters in flight (PRs may be open by the time you read this):

| Stub | Issue | In-flight PR | Strategy |
|---|---|---|---|
| `_NullSceneScorer` | #166 | coder dispatched | CLIP-ViT-B-32 image-encoder cosine distance |
| `_NullAudioVisualConflictScorer` | #168 | coder dispatched | Heuristic (VAD RMS × lip-region pixel-diff via OpenCV Haar) — per RFC #236 |
| `_NullDeicticModel` | #169 | coder dispatched | MiniCPM.chat() reuse with yes/no prompt — per RFC #237 |
| `_NullUrgencyScorer` | #171 | coder dispatched | Prosody (speech rate + RMS) + lexicon (13 distress phrases) composite — per RFC #238 |
| `_NullGroundingModel` | #172 | coder dispatched | `IDEA-Research/grounding-dino-tiny` |
| `_NullEmbeddingAdapter` | #183 | coder dispatched | `sentence-transformers/all-MiniLM-L6-v2` |
| `_NullProvenanceComputer` (in SleepTimeAgent) | #188 | PR #235 awaiting gatekeeper | `MiniCPMProvenanceComputer` reuses MiniCPM.chat() |
| Hardcoded `attachment_risk_level=0.0` in live builder | #213 | PR #234 awaiting gatekeeper | Wire `EventStreamAttachmentRiskMonitor.current_level()` |

Stub markers (`# UNAVAILABLE: #N`) are enforced by `tests/test_unavailable_markers_have_issues.py` — every marker must cite a real open issue.

---

## In-flight PRs (as of this snapshot)

| PR | Title | State |
|---|---|---|
| #233 | Eval Phase D: VoiceBench adapter | gatekeeper running |
| #234 | wire `EventStreamAttachmentRiskMonitor` into live PolicyInputs (closes #213) | gatekeeper running |
| #235 | `MiniCPMProvenanceComputer` for `SleepTimeAgent` (closes #188) | gatekeeper running |
| #239 | docs: `model-stack.md` reference | awaiting merge |
| #241 | `EventLogger.late_subscribe()` public API (recovery PR) | awaiting gatekeeper |

Stub-realization PRs may be opened by re-dispatched coders for #166/#168/#169/#171/#172/#183 and HumDial-FDBench by the time you read this. Check `gh pr list`.

PR #240 (`EmptyMemoryStore.commit()` returns `CommitResult.COMMITTED`) merged 2026-05-15; fixed a pre-existing main test failure.

---

## Where the code lives

```
companion_harness/        # runtime
  speak_policy.py         # T3 — rule-based, POLICY_VERSION = v0.1j
  realtime_orchestrator.py
  event_logger.py
  replay.py               # Tier-B replayer (run_tier_b_replay, assert_bit_identical)
  schemas.py
  reason_codes.py
  memory_manager.py       # 4-store interface + CommitResult enum
  sleep_time_agent.py
  attachment_risk_monitor.py
  aesthetic_rubric.py
  vision_sidecar.py       # Protocol seams for scene/AV-conflict/deictic/grounding
  foreground_model_minicpm.py
  tts_kokoro.py / tts_minicpm_native.py
  addressing_classifier.py    # MiniCPM primary + WakeWord safety net
  native_duplex_eou.py        # MiniCPM native_duplex EOU source
  fast_tool_dispatcher.py + tool_progress.py
  background_reasoner.py      # FakeBackgroundReasoner; real LLM is v0.1f+1
  evals/                  # ENTIRELY downstream; runtime must not import
    adapters/             # harness_native, candor, full_duplex_bench, voicebench, vocalbench, humdial_fdbench
    reporters/            # json, md, distributional_md
    scenarios/            # synthetic_clock, audio_feeder, fixture
    schemas/
    metrics/
    extractors/

manual_test_console/      # the rig
  server.py               # aiohttp; routes / + /healthz + /ws/* + /config/*
  index.html              # 3-column UI: left events / middle display / right tuning
  live_pipeline.py        # per-session factory: orchestrator + detectors + ASR + memory + policy
  config_store.py + config_schema.py + config.yaml

docs/
  architecture-v0.1.md            # FROZEN — never edit
  manual-test-handbook.md         # operator runbook
  model-stack.md                  # adapter inventory
  project-progress-2026-05-15.md  # this file
  eval-quickstart.md
  eval-subsystem-spec.md
  roadmap-v0.1[a-j]-draft.md
  plan-*.md                       # converged plans for various wirings
  remote-dev.md
  design-config-and-dashboard.md
```

---

## Conventions to know

1. **Adapter-first** (`CLAUDE.md`): every model lives behind a Protocol in `companion_harness/`. `speak_policy.py` + tests MUST NOT import a model SDK. The adapter is where ablation + replay happen.
2. **`# UNAVAILABLE: #N` markers** cite open issues; enforced by `tests/test_unavailable_markers_have_issues.py`.
3. **Regression-grep** before every push (per `CLAUDE.md` pre-flight): grep for `^-[^-].*(13-token-watch-list)` to catch silent reverts of merged-PR symbols.
4. **Worktree discipline**: never edit main working tree (`/home/yid042/projects/companion-agent-harness`); always `git worktree add /tmp/wt-<name> origin/main` first. Multiple sessions can run in parallel without interfering.
5. **Canonical venv** for pytest: `/raid/yid042/venvs/companion-harness/bin/python3`. GPU tests marked `@pytest.mark.gpu`; deselect with `-m "not gpu"`.
6. **POLICY_VERSION monotonic**: never downgrade. Current = `v0.1j`.
7. **`caused_by[]` closure**: every event must cite a predecessor. Orphan events fail Stage 0 contract tests.
8. **POLICY path is bit-identical Tier-B replay**: `companion_harness/replay.py::assert_bit_identical()` is the gate.
9. **Eval is downstream**: runtime never imports `companion_harness.evals.*`. Enforced by `tests/test_runtime_does_not_import_evals.py`.

---

## Open issues worth knowing about

| Issue | Title |
|---|---|
| #157 | UNAVAILABLE: signals from native_duplex / addressing fallback / TTS native (referenced by various stubs) |
| #166 | CLIP scene-change scorer (in flight) |
| #168 | AV-conflict scorer (in flight; heuristic baseline) |
| #169 | Deictic detector (in flight; MiniCPM reuse) |
| #171 | Urgency scorer (in flight; prosody+lexicon) |
| #172 | Grounding model (in flight; Grounding-DINO-tiny) |
| #183 | Embedding adapter (in flight; sentence-transformer) |
| #188 | LLM-driven provenance (PR #235 awaiting gatekeeper) |
| #213 | Wire EventStreamAttachmentRiskMonitor live (PR #234 awaiting gatekeeper) |
| #236 | RFC: AV-conflict model selection |
| #237 | RFC: Deictic model selection |
| #238 | RFC: Urgency model selection |

Check `gh issue list --repo Eden-kk/companion-agent-harness --state open` for the live set.

---

## What to do if the operator says "help me test"

1. Confirm the server is up: `ss -ltnp | grep :8800` (Linux) or `lsof -i :8800`.
2. If not running, start it (commands above). Wait for "live pipeline ready" in the log (~60-120 s).
3. Walk them through scenarios A–N in [`manual-test-handbook.md`](manual-test-handbook.md) §2.4 + §3.3.
4. Watch the event panel together. For each scenario, confirm the expected events fire.
5. If something unexpected happens: save the session ID + blob path; open an issue tagged `manual-test-finding`.
6. The dashboard (right column) is fair game for live tuning — every patch is audit-logged, so experiments are recoverable.

Do NOT:
- Modify spec (`architecture-v0.1.md` is FROZEN).
- Bump POLICY_VERSION without a milestone task that says to.
- Skip the worktree pre-flight when making code changes.
- Use `git checkout` to switch the user's main working tree to another branch.
- Run pytest with bare `python3`; always use `/raid/yid042/venvs/companion-harness/bin/python3`.

---

## Quick-reference URLs

- Console: `http://localhost:8800/`
- Health: `http://localhost:8800/healthz`
- Config snapshot: `http://localhost:8800/config`
- WebSockets: `/ws/ingest` (browser → harness), `/ws/display` (harness → browser events), `/ws/audio_out` (harness → browser audio)
- Repo: `Eden-kk/companion-agent-harness`
