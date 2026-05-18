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
        │  • SmartTurn v3        (interim primary)     │──► vad_signal_suppressed_by_smart_turn
        │      (vetoes VAD when p_continue > p_done;   │     (per-frame veto, PR #264)
        │       both detectors still advance state)    │
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
        │  T2 — Addressing classifier (PR #263)        │──► addressing_classified
        │  • Tier 1: WakeWord ("Claude"/"Claudia")     │──► signal_producer_fallback
        │  • Tier 2: MiniCPM confidence=="explicit"    │     (on fallback)
        │  • Tier 3: implicit — requires ≥3 tokens AND │
        │    not in WHISPER_HALLUCINATION_DENYLIST     │
        │    (16 phrases); default False               │
        │    (silence wins ties — invariant #8)        │
        └──────────────────┬───────────────────────────┘
                           │ user_addressed_agent
                           ▼
        ┌──────────────────────────────────────────────┐
        │  T2 — Memory.retrieve(query=transcript)      │──► memory_retrieval_event
        │  • lexical baseline (default)                │
        │  • SentenceTransformer (opt-in, PR #247)     │
        └──────────────────┬───────────────────────────┘
                           │ context_items: tuple[MemoryItem, ...]
                           ▼
   ────────────────────────────────────────────────────  side-channels merge here
        ┌──────────────────────────────────────────────┐
        │  T2 — VisionSidecar (--enable-vision)        │
        │  • scene_change_score  (CLIP, PR #248)       │
        │  • grounding_confidence (Grounding-DINO #250)│
        │  • audio_visual_conflict (heuristic #242)    │
        │  • deictic_reference/ambiguous (MiniCPM #246)│
        └──────────────────┬───────────────────────────┘
                           │
        ┌──────────────────▼───────────────────────────┐
        │  T2 — urgency_score (prosody+lexicon #244)   │
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

| # | Component | Stage | Backend | Adapter file | default-on status (v0.2e) |
|---|---|---|---|---|---|
| 1 | Silero VAD | T1 detector — VAD safety net | PyTorch `silero-vad` | `companion_harness/turn_detector_vad.py` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 2 | SmartTurn v3 | T1 detector — turn-end (interim primary) | Pipecat mLSTM weights | `companion_harness/turn_detector_smart.py` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 3 | MiniCPM-o native_duplex EOU | T1 EOU — final primary | `MiniCPMODuplex.streaming_generate()` `is_listen` | `companion_harness/native_duplex_eou.py::MiniCPMNativeDuplexEouSource` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 4 | Backchannel classifier | T1 backchannel | faster-whisper-tiny + curated lexicon | `companion_harness/backchannel_asr_lexicon.py` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 5 | Whisper-tiny ASR | T2 ASR | `faster-whisper` (cu128) | `companion_harness/asr_faster_whisper.py` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 6 | MiniCPM addressing classifier | T2 addressing — primary | `MiniCPMDuplexModel.chat(yes/no prompt)` | `companion_harness/addressing_classifier.py::MiniCPMAddressingClassifierImpl` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 7 | Wake-word addressing | T2 addressing — safety net | regex on `"Claude" / "Claudia"` | `companion_harness/addressing_classifier.py::WakeWordAddressingClassifier` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 8 | MiniCPM-o foreground | T4 proposal generation | `MiniCPM-o-2_6` (init_vision=audio=tts=True) | `companion_harness/foreground_model_minicpm.py` | out of scope @ v0.2e (different rollout regime; no `--enable-X` gate) |
| 9 | Kokoro TTS | T4 TTS — default | Kokoro-82M-ONNX (onnxruntime) | `companion_harness/tts_kokoro.py` | out of scope @ v0.2e (governed by `--tts-adapter`; libcudart gate) |
| 10 | MiniCPM-o native TTS | T4 TTS — opt-in | `MiniCPMO.chat(generate_audio=True)` | `companion_harness/tts_minicpm_native.py` | out of scope @ v0.2e (governed by `--tts-adapter`; libcudart gate) |
| 11 | CosyVoice2 TTS | T4 TTS — bilingual opt-in | CosyVoice2-0.5B (FunAudioLLM); Apache 2.0; 9 languages incl. zh+en; inference_sft default, zero-shot via `--cosyvoice-reference-wav` | `companion_harness/tts_cosyvoice2.py` | select via `--tts-adapter cosyvoice2`; install from git clone (not PyPI 0.0.8 — torch conflict); weights at `/raid/yid042/models/cosyvoice2/CosyVoice2-0.5B/`; pynini via conda |
| 11 | EventStreamAttachmentRiskMonitor | parallel — risk | event-stream heuristic (3 sub-categories); wired live (PR #234) | `companion_harness/attachment_risk_monitor.py` | deferred @ v0.2e → v0.3 (OQ-3: flag posture unresolved) |
| 12 | RegexAestheticRubric | T3 rubric gate | regex/lexicon (8 violation IDs) | `companion_harness/aesthetic_rubric.py` | out of scope @ v0.2e (always-on; no `--enable-X` gate) |
| 13 | `CLIPSceneChangeScorer` | T2 vision — scene change | openai/clip-vit-base-patch32, CPU-default (PR #248) | `companion_harness/clip_scene_scorer.py` | default-on @ v0.2e (`--enable-clip-scene` default=True) |
| 14 | `HeuristicAVConflictScorer` | T2 vision — A/V conflict | VAD RMS × OpenCV Haar lip-region pixel-diff (PR #242) | `companion_harness/av_conflict_scorer_heuristic.py` | default-on @ v0.2e (`--enable-av-conflict` default=True) |
| 15 | `MiniCPMDeicticDetector` | T2 vision — deictic | `MiniCPM.chat()` yes/no — text-only (no image arg in current chat() API) (PR #246) | `companion_harness/deictic_detector_minicpm.py` | default-on @ v0.2e (`--enable-deictic` default=True) |
| 16 | `ProsodyLexiconUrgencyScorer` | T2 urgency | 13 distress phrases + RMS + speech-rate composite (PR #244) | `companion_harness/urgency_scorer_prosody_lexicon.py` | default-on @ v0.2e (`--enable-urgency` default=True) |
| 17 | `GroundingDINOAdapter` | T2 vision — grounding | IDEA-Research/grounding-dino-tiny (PR #250) | `companion_harness/grounding_dino_adapter.py` | default-on @ v0.2e (`--enable-grounding` default=True) |
| 18 | `SentenceTransformerEmbedder` | T2 memory — embeddings | sentence-transformers/all-MiniLM-L6-v2, CPU-default (PR #247) | `companion_harness/embedder_sentence_transformer.py` | default-on @ v0.2e (`--enable-embeddings` default=True) |
| 19 | `MiniCPMProvenanceComputer` | parallel — SleepTimeAgent | `MiniCPM.chat()` for confidence/salience/summary (PR #235) | `companion_harness/provenance_minicpm.py` | out of scope @ v0.2e (SleepTimeAgent path; no `--enable-X` gate) |

Live-pipeline wiring default-ON since v0.2e: `--enable-clip-scene`, `--enable-grounding`,
`--enable-av-conflict`, `--enable-deictic`, `--enable-urgency`, `--enable-embeddings`.
Use `--no-enable-X` to revert any to OFF. `--enable-vision` remains OFF by default (+18 GB VRAM).

---

## Stub seams — no permanent stubs remain

**All 8 previously-tracked stubs are realized.** Each `_Null*` Protocol
fallback fires only when (a) the corresponding `--enable-*` CLI flag is OFF,
(b) the optional dependency is missing (e.g. `transformers`, `opencv-python`,
`sentence-transformers`), or (c) the deictic startup hook can't reuse the
foreground model (falls back to null).

| # | Realized adapter | Stage | Previously stub | Closed by |
|---|---|---|---|---|
| S1 | `CLIPSceneChangeScorer` | T2 vision | `_NullSceneScorer` (#166) | PR #248 |
| S2 | `HeuristicAVConflictScorer` | T2 vision | `_NullAudioVisualConflictScorer` (#168) | PR #242 |
| S3 | `MiniCPMDeicticDetector` | T2 vision | `_NullDeicticModel` (#169) | PR #246 |
| S4 | `ProsodyLexiconUrgencyScorer` | T2 urgency | `_NullUrgencyScorer` (#171) | PR #244 |
| S5 | `GroundingDINOAdapter` | T2 vision | `_NullGroundingModel` (#172) | PR #250 |
| S6 | `SentenceTransformerEmbedder` | T2 memory | `_NullEmbeddingAdapter` (#183) | PR #247 |
| S7 | `MiniCPMProvenanceComputer` | parallel | SleepTimeAgent null provenance (#188) | PR #235 |
| S8 | `EventStreamAttachmentRiskMonitor` wired live | T2 | hardcoded `attachment_risk_level=0.0` (#213) | PR #234 |

The `# UNAVAILABLE: #<issue>` marker convention remains enforced by
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
| T1 turn detection | audio chunks | `TurnSignal` | `vad_speech_*`, `turn_signal`, `turn_signal_coalesced` (PR #260), `native_duplex_invocation`, `signal_producer_fallback`, `vad_signal_suppressed_by_smart_turn` (PR #264) |
| T2 ASR | audio window | `transcript: str` | `asr_transcript_emitted` (verified live, PR #140) |
| T2 addressing | transcript | `user_addressed_agent: bool` | `addressing_classified`, `signal_producer_fallback` |
| T2 memory retrieve | transcript | `tuple[MemoryItem]` | `memory_retrieval_event` |
| T2 vision | frame bytes | scene / deictic / grounding / conflict | `vision_frame` |
| T3 SpeakPolicy.decide | `PolicyInputs` | `SpeakDecision` + `DecisionTrace` | `policy_decision` |
| T4 tool dispatch | `SpeakDecision(tool_call)` | tool result | `tool_call_requested/dispatched/progress/completed/cancelled` |
| T4 foreground | `(audio, video, context_items)` | proposal text | `foreground_proposal` |
| T4 TTS | proposal text | audio bytes | `tts_audio_emitted`, `tts_synthesis_started/completed/cancelled` (PR #258) |
| AudioOutputController | audio bytes | playback | `assistant_audio_buffer_queued` (per-chunk audit, PR #258), `assistant_audio_buffer_flushed` |
| Parallel — SleepTimeAgent | event stream | committed `MemoryItem` | `memory_commit_completed`, `memory_commit_skipped`, `memory_tombstone_completed` (PR #253), `explicit_forget` (PR #253) |
| Parallel — Config dashboard | HTTP `/config/patch` | mutated ConfigStore | `config_change`, `operator_action` |

---

## AddressingClassifier semantics (PR #263)

The classifier is a three-tier waterfall. Tier-3 was rewritten to default
**False** (was: solo-fallback → True, which produced Finding 12 false
positives on whisper-tiny hallucinations).

- **Tier 1 — WakeWord** (regex on `"Claude"` / `"Claudia"`): direct True.
- **Tier 2 — MiniCPM**: `confidence == "explicit"` → direct True.
- **Tier 3 — implicit**: requires substantive transcript (≥ 3 tokens AND
  not in `WHISPER_HALLUCINATION_DENYLIST` of 16 phrases such as `"you"`,
  `"Thanks for watching"`, `"Bye"`, etc.). Otherwise **False**.
  Spec-consistent per invariant #8 (silence wins ties).

---

## DecisionTrace fields (PR #262)

The `DecisionTrace` schema gained two transcript-audit fields so policy
decisions can be reviewed against the input that drove them:

- `user_transcript: SensitiveField | None` — the transcript that drove the
  decision (`retention_policy_id="transcript_audit_30d"`,
  `sensitivity="sensitive"`).
- `user_transcript_preview: str | None` — first 50 characters,
  non-sensitive, suitable for cheap audit grep without unsealing the
  `SensitiveField`.

---

## Recent fixes (Round-4 sweep)

| PR | Subject |
|---|---|
| #253 | Memory tombstone path + `explicit_forget` trigger |
| #257 | Session leak fix |
| #258 | TTS / audio-buffer observability events |
| #260, #265 | Orchestrator burst-dispatch dedup (`turn_signal_coalesced`) |
| #262 | Transcript fields added to `DecisionTrace` |
| #263 | AddressingClassifier tier-3 defaults False (Finding 12) |
| #264 | SmartTurn vetoes VAD on `p_continue > p_done` (Finding 13) |

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
