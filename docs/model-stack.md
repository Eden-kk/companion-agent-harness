# Model stack reference

This is a **snapshot reference**: which models exist in the system today,
where they sit in the audio flow, what they consume, what they emit, and
which seams are still stubs.

Read [`architecture-v0.1.md`](architecture-v0.1.md) for the **why** (spec
invariants, stage definitions, gate rationale). Read this file for the
**what** (concrete adapters, model IDs, files, issue refs).

For real-time correctness behavior, every Protocol seam is **adapter-first**
per `CLAUDE.md` — `speak_policy.py` and test files MUST NOT import any
model SDK directly. The adapter is where ablation, replay, and stub
swapping happen.

---

## Audio flow (end-to-end)

```
        [Mic / WebSocket / DirectAudioInputFeeder]
                       │
                       ▼  raw_audio_chunk events
        ┌──────────────────────────────────────────────┐
        │  T1 — Turn-detector suite (parallel)         │
        │  • Silero VAD          (safety net)          │
        │  • SmartTurn v3        (interim primary)     │
        │  • MiniCPM native_duplex (final primary EOU) │──► native_duplex_invocation
        │  • Backchannel (whisper-tiny + lexicon)      │
        └──────────────────┬───────────────────────────┘
                           │ TurnSignal
                           ▼
        ┌──────────────────────────────────────────────┐
        │  T2 — ASR  (faster-whisper-tiny)             │──► asr_transcript_emitted
        └──────────────────┬───────────────────────────┘
                           │ transcript: str
                           ▼
        ┌──────────────────────────────────────────────┐
        │  T2 — Addressing classifier                  │──► addressing_classified
        │  • MiniCPM-derived  (primary, real)          │──► signal_producer_fallback
        │  • WakeWord       (safety-net, regex)        │     (on fallback)
        │  • social_mode fallback (mechanical)         │
        └──────────────────┬───────────────────────────┘
                           │ user_addressed_agent
                           ▼
        ┌──────────────────────────────────────────────┐
        │  T2 — Memory.retrieve(query=transcript)      │──► memory_retrieval_event
        │  • lexical baseline (default)                │
        │  • SentenceTransformer (opt-in, PR #233)     │
        └──────────────────┬───────────────────────────┘
                           │ context_items: tuple[MemoryItem, ...]
                           ▼
   ────────────────────────────────────────────────────  side-channels merge here
        ┌──────────────────────────────────────────────┐
        │  T2 — VisionSidecar (--enable-vision)        │
        │  • scene_change_score (stub #166)            │
        │  • grounding_confidence (stub #172)          │
        │  • audio_visual_conflict (stub #168)         │
        │  • deictic_reference/ambiguous (stub #169)   │
        └──────────────────┬───────────────────────────┘
                           │
        ┌──────────────────▼───────────────────────────┐
        │  T2 — urgency_score (stub #171)              │
        │  T2 — attachment_risk (real monitor + wiring)│
        └──────────────────┬───────────────────────────┘
                           ▼
        ┌──────────────────────────────────────────────┐
        │  T3 — SpeakPolicy.decide()                   │──► policy_decision
        │  POLICY_VERSION = v0.1j                      │──► DecisionTrace
        │  8 decision paths; all set                   │
        │    response_content_source.                  │
        │  Gates active: RUBRIC_VIOLATION,             │
        │   ATTACHMENT_RISK_DAMPEN,                    │
        │   tool_status filler/silence,                │
        │   VISUAL_LOW_CONFIDENCE, COOLDOWN,           │
        │   BUDGET_EXHAUSTED, SILENCE_WINS_TIES.       │
        └──────┬──────────────┬────────────────────────┘
               │              │              │
               ▼              ▼              ▼
         [silence]      [tool_call]    [speak proposal]
                             │              │
                             ▼              ▼
                  ┌──────────────────┐  ┌─────────────────────────────┐
                  │ FastToolDispatch │  │ T4 — ForegroundModel        │
                  │ + ProgressEmitter│  │  • MiniCPM-o (init_vision,  │
                  │ + BackgroundReas │  │    init_audio, init_tts ALL)│
                  │ + 300ms barge-in │  │  • process_stream(audio,    │
                  └──────────────────┘  │    video, context_items)    │
                                        └──────────────┬──────────────┘
                                                       │ proposal_text
                                                       ▼
                                        ┌──────────────────────────────┐
                                        │ T4 — TTS                     │
                                        │ • Kokoro-82M-ONNX (default)  │
                                        │ • MiniCPM-o native (opt-in)  │
                                        └──────────────┬───────────────┘
                                                       ▼ audio bytes
                                          AudioOutputController
                                                       │
                                                       ▼
                                        assistant_audio_buffer_flushed
                                                       │
                                                       ▼
                                                  [Speaker]

PARALLEL / OFF-PATH:
  • SleepTimeAgent (opt-in via wire_sleep_time_agent=True)
      Subscribes via EventLogger.late_subscribe.
      Batches memory_write_candidate → commits to 4 stores.
      Provenance: null stub or MiniCPMProvenanceComputer.
  • EventStreamAttachmentRiskMonitor (real, event-stream-only).
  • ConfigStore (HTTP /config/patch → 12 Tier-B knobs, live tuning).
  • EventLogger (async non-blocking; caused_by closure for every event).
```

---

## Real models (operational)

| # | Component | Stage | Backend | Adapter file |
|---|---|---|---|---|
| 1 | Silero VAD | T1 detector — VAD safety net | PyTorch `silero-vad` | `companion_harness/turn_detector_vad.py` |
| 2 | SmartTurn v3 | T1 detector — turn-end (interim primary) | Pipecat mLSTM weights | `companion_harness/turn_detector_smart.py` |
| 3 | MiniCPM-o native_duplex EOU | T1 EOU — final primary | `MiniCPMODuplex.streaming_generate()` `is_listen` | `companion_harness/native_duplex_eou.py::MiniCPMNativeDuplexEouSource` |
| 4 | Backchannel classifier | T1 backchannel | faster-whisper-tiny + curated lexicon | `companion_harness/backchannel_asr_lexicon.py` |
| 5 | Whisper-tiny ASR | T2 ASR | `faster-whisper` (cu128) | `companion_harness/asr_faster_whisper.py` |
| 6 | MiniCPM addressing classifier | T2 addressing — primary | `MiniCPMDuplexModel.chat(yes/no prompt)` | `companion_harness/addressing_classifier.py::MiniCPMAddressingClassifierImpl` |
| 7 | Wake-word addressing | T2 addressing — safety net | regex on `"Claude" / "Claudia"` | `companion_harness/addressing_classifier.py::WakeWordAddressingClassifier` |
| 8 | MiniCPM-o foreground | T4 proposal generation | `MiniCPM-o-2_6` (init_vision=audio=tts=True) | `companion_harness/foreground_model_minicpm.py` |
| 9 | Kokoro TTS | T4 TTS — default | Kokoro-82M-ONNX (onnxruntime) | `companion_harness/tts_kokoro.py` |
| 10 | MiniCPM-o native TTS | T4 TTS — opt-in | `MiniCPMO.chat(generate_audio=True)` | `companion_harness/tts_minicpm_native.py` |
| 11 | EventStreamAttachmentRiskMonitor | parallel — risk | event-stream heuristic (3 sub-categories) | `companion_harness/attachment_risk_monitor.py` |
| 12 | RegexAestheticRubric | T3 rubric gate | regex/lexicon (8 violation IDs) | `companion_harness/aesthetic_rubric.py` |

---

## Stub seams — Protocol wired, real model pending

Each row is a Protocol-conforming stub returning a neutral value. Real
adapters drop in behind the same Protocol surface; no policy/replay-side
change required.

| # | Component | Stage | Stub returns | Issue | Final-product target |
|---|---|---|---|---|---|
| S1 | `_NullSceneScorer` | T2 vision | `0.0` | #166 | CLIP image-encoder cosine distance |
| S2 | `_NullAudioVisualConflictScorer` | T2 vision | `0.0` | #168 | TBD — see RFC issue #236 |
| S3 | `_NullDeicticModel` | T2 vision | `(False, 0.0)` | #169 | MiniCPM-o.chat() reuse (per RFC #237) |
| S4 | `_NullUrgencyScorer` | T2 urgency | `0.0` | #171 | Prosody + lexicon composite (per RFC #238) |
| S5 | `_NullGroundingModel` | T2 vision | `0.0` | #172 | Grounding-DINO-tiny |
| S6 | `_NullEmbeddingAdapter` | T2 memory | `[]` (lexical fallback) | #183 | sentence-transformers/all-MiniLM-L6-v2 |
| S7 | SleepTimeAgent null provenance | parallel | `(0.8, 0.5, passthrough)` | #188 | `MiniCPMProvenanceComputer` |
| S8 | Live builder hardcoded `attachment_risk_level=0.0` | T2 | `0.0` literal | #213 | Wire `EventStreamAttachmentRiskMonitor` |

The `# UNAVAILABLE: #<issue>` marker on each stub is enforced by
`tests/test_unavailable_markers_have_issues.py` against the
`KNOWN_UNAVAILABLE_ISSUES` allowlist — every marker must cite a real open
issue. When a stub is replaced by a real adapter, the issue closes and is
removed from the allowlist.

---

## Non-model components (policy, orchestration, audit)

| # | Component | Stage | Notes |
|---|---|---|---|
| N1 | `SpeakPolicy.decide()` | T3 | Pure rule-based; POLICY_VERSION = v0.1j; 8 paths; bit-identical Tier-B replay (invariant #5) |
| N2 | `FastToolDispatcher` | T4 tool routing | Local-tool registry, deterministic `tool_call_id` via SHA-256 |
| N3 | `ToolProgressEmitter` | T4 tool routing | Per-tool filler budget (2 fillers, 4s gap); pure `evidence_at()` for replay |
| N4 | `FakeBackgroundReasoner` | T4 smart-path | Deterministic fake (real MCP/LLM reasoner = v0.1f+1) |
| N5 | `ConfigStore` | parallel — runtime tuning | 12 Tier-B knobs; dashboard HTTP patch + `config_change`/`operator_action` events |
| N6 | `EventLogger` | parallel — audit | Async non-blocking; `caused_by[]` closure; `late_subscribe()` API |
| N7 | `Tier-B Replayer` | offline | `companion_harness/replay.py` — `run_tier_b_replay()` + `assert_bit_identical()` |
| N8 | `MemoryManager` (4 stores) | parallel | session_state / core_user_profile / episodic_memory / semantic_relational |

---

## Eval subsystem (offline; `companion_harness/evals/`)

| # | Component | Mode | File |
|---|---|---|---|
| E1 | `harness_native` adapter | Real (wraps 4 contract tests) | `evals/adapters/harness_native.py` |
| E2 | `FixtureScenarioDriver` + SyntheticClock + DirectAudioInputFeeder | Real (Phase A.5) | `evals/scenarios/` |
| E3 | `CandorCaseSource` | Synthetic (real CANDOR data deferred) | `evals/adapters/candor.py` |
| E4 | `FullDuplexBenchV1/V1.5CaseSource` | Synthetic (real FDB data deferred) | `evals/adapters/full_duplex_bench.py` |
| E5 | `CausalFailureSliceExtractor` | Real (BFS over `caused_by[]`) | `evals/extractors/` |
| E6 | `VoiceBench` / `VocalBench` / `HumDial-FDBench` adapters | Synthetic-mode default | `evals/adapters/voicebench.py`, `vocalbench.py`, `humdial_fdbench.py` |

**Import-direction invariant** (enforced by
`tests/test_runtime_does_not_import_evals.py`): runtime code MUST NOT
import from `companion_harness.evals.*`. Eval is downstream of runtime,
never upstream.

---

## Inputs / outputs per stage

| Stage | Reads | Produces | Events emitted |
|---|---|---|---|
| Ingestion | mic / WS bytes | `raw_audio_chunk` | `raw_audio_chunk`, `raw_video_frame` |
| T1 turn detection | audio chunks | `TurnSignal` | `vad_speech_*`, `turn_signal`, `native_duplex_invocation`, `signal_producer_fallback` |
| T2 ASR | audio window | `transcript: str` | `asr_transcript_emitted` |
| T2 addressing | transcript | `user_addressed_agent: bool` | `addressing_classified`, `signal_producer_fallback` |
| T2 memory retrieve | transcript | `tuple[MemoryItem]` | `memory_retrieval_event` |
| T2 vision | frame bytes | scene / deictic / grounding / conflict | `vision_frame` |
| T3 SpeakPolicy.decide | `PolicyInputs` | `SpeakDecision` + `DecisionTrace` | `policy_decision` |
| T4 tool dispatch | `SpeakDecision(tool_call)` | tool result | `tool_call_requested/dispatched/progress/completed/cancelled` |
| T4 foreground | `(audio, video, context_items)` | proposal text | `foreground_proposal` |
| T4 TTS | proposal text | audio bytes | `tts_audio_emitted` |
| AudioOutputController | audio bytes | playback | `assistant_audio_buffer_queued/flushed` |
| Parallel — SleepTimeAgent | event stream | committed `MemoryItem` | `memory_commit_completed`, `memory_commit_skipped` |
| Parallel — Config dashboard | HTTP `/config/patch` | mutated ConfigStore | `config_change`, `operator_action` |

---

## Adapter-first discipline

Every real model lives in `companion_harness/<name>_<backend>.py` behind a
Protocol declared next to its stub. Adding a new model means:

1. Pick or extend an existing Protocol in `companion_harness/<stage>.py`.
2. Write the adapter in a new file `companion_harness/<adapter>.py`.
3. Write contract tests in `tests/test_<adapter>.py` (mark `@pytest.mark.gpu` if model-loading).
4. Wire into `manual_test_console/live_pipeline.py::build_live_pipeline` as an opt-in kwarg.
5. Run regression-grep on the 13-token watch list (see `CLAUDE.md`).
6. Do NOT bump POLICY_VERSION unless the policy path itself changes.

---

## Cross-references

- **Spec:** [`architecture-v0.1.md`](architecture-v0.1.md) (FROZEN; never edit).
- **Coding discipline:** [`../CLAUDE.md`](../CLAUDE.md) — 4 rules + project rules + pre-flight worktree discipline.
- **Roadmaps:** `roadmap-v0.1[a–j]-draft.md` — milestone definitions, locked anchors, OQ resolutions.
- **Eval spec:** [`eval-subsystem-spec.md`](eval-subsystem-spec.md).
- **Manual-test handbook:** [`manual-test-handbook.md`](manual-test-handbook.md) — operator playbook + `--enable-vision` flag.
- **Remote dev:** [`remote-dev.md`](remote-dev.md) — b200 setup.
- **Memory plan follow-up:** [`plan-memory-wiring-followup.md`](plan-memory-wiring-followup.md).
- **Vision plan:** [`plan-vision-sidecar-wiring.md`](plan-vision-sidecar-wiring.md).
