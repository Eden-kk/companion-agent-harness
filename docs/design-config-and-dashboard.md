# design: config extraction + manual-test dashboard (Tier A/B/C, replay-safe)

> Status: **design only**. No code change accompanies this document. Phase rollout in §9.
>
> Architecture references: `docs/architecture-v0.1.md` Part 2 (invariants), Part 6 Stage 0 (replay), Part 6 Stage 3 (speak-policy), Part 8 (v0.1a/v0.1b numeric gates), Part 9 (implementation config; not part of the architecture).

---

## §0 — Goals + non-goals

**Goals.**

- G1. Reduce friction for the project lead when tuning detector/policy thresholds during a manual-test session. Today a threshold change requires editing source, restarting the b200 server, and re-loading every adapter — minutes of latency per "what if we made backchannel less aggressive?" turn-around.
- G2. Make the harness's parameter state honest and audit-traceable. Each runtime threshold change MUST be itself an `Event` in the same log as every other behavior (invariant #1 — "no unlogged behavior"; spec lines 41–43). The replay system MUST be able to reconstruct which threshold values were in effect at the moment of any policy decision.

**Non-goals.**

- N1. Dynamic reconfiguration of spec-pinned numeric gates (Part 8 lines 906–924). These are FROZEN — they exist to gate releases, not to be tuned mid-session.
- N2. A polished production admin UI. The manual-test console is a developer rig; the dashboard is for the project lead in front of an ssh tunnel, not a customer.
- N3. Persisting tuned config across server restarts. Per-session ephemeral state only; restart returns to defaults from code. (See §9 Phase 3 if this changes.)
- N4. Authentication / authorization on `/config/patch`. The console is gated by `ssh -L` (manual-test-handbook.md §0), not by the HTTP layer.

---

## §1 — Parameter classification framework

Every tunable number in `companion_harness/` belongs to exactly one of three tiers. Classification is per-parameter; a single module may contain parameters from all three tiers.

### Tier A — FROZEN (spec gates / determinism-affecting)

Cannot be tuned at runtime. Cannot be tuned via dashboard. Cannot appear in `manual_test_console/config.yaml`. Listed here so the implementation has an explicit allowlist for the rejection check in §7.

| Name | Value | Source | Spec source | Rationale |
|---|---|---|---|---|
| `physical_user_speech_onset_to_stop_ms_p95` | < 350 ms (v0.1a) → < 250 ms (v0.1b) | hard gate, not a runtime knob | `docs/architecture-v0.1.md:914-915` | Spec acceptance gate. |
| `direct_question_latency_p50` | < 800 ms | hard gate | `docs/architecture-v0.1.md:911` | Spec acceptance gate. |
| `direct_question_latency_p95` | < 1500 ms | hard gate | `docs/architecture-v0.1.md:912` | Spec acceptance gate. |
| `vad_detected_user_speech_to_stop_ms_p95` | < 200 ms | hard gate | `docs/architecture-v0.1.md:913` | Spec acceptance gate. |
| `false_interruption_count_per_10_min` | < 1 | hard gate | `docs/architecture-v0.1.md:916` | Spec acceptance gate. |
| ASR `temperature` | 0.0 | `companion_harness/asr_faster_whisper.py:55` | spec invariant #5 (determinism) | Greedy decoding; sampling would break replay. |
| ASR `beam_size` | 1 | `companion_harness/asr_faster_whisper.py:56` | spec invariant #5 | Beam variance breaks replay. |
| ASR `condition_on_previous_text` | False | `companion_harness/asr_faster_whisper.py:57` | spec invariant #5 | Auto-regressive variance breaks replay. |
| ASR `language` | "en" | `companion_harness/asr_faster_whisper.py:54` | invariant #5 | Pinned (whisper-tiny.en is English-only). |
| `POLICY_VERSION` | "v0.1d" | `companion_harness/speak_policy.py:19` | invariant #5 | Bumped only by intentional spec migration. |
| `CONFIG_VERSION` | "v0.1e" | `companion_harness/speak_policy.py:20` | invariant #5 | Bumped only by intentional spec migration. |
| `ProgressStage` literal alphabet | locked set | `companion_harness/v0_1f_event_schema.py:30,111` (refers to `companion_harness.tool_progress.ProgressStage`) | v0.1f Anchor 1 | Closed alphabet — alphabet renames cost reclassification. |
| `RubricViolation` literal alphabet | 8 IDs locked | `companion_harness/schemas.py:55-64` | v0.1g Anchor 1 (`docs/roadmap-v0.1g-draft.md:40-41`) | Closed alphabet. |
| `_LEVEL_TO_FLOAT_THRESHOLD` map (low / medium / high → 0.3 / 0.6 / 0.85) | 0.3 / 0.6 / 0.85 | `companion_harness/speak_policy_config.py:117-121` | spec Part 6 Stage 3 (`docs/architecture-v0.1.md:475-488`) | The MAPPING is calibration (Tier C-ish), but the LEVELS themselves are part of the policy contract carried in `DecisionTrace`. Treat as Tier A until §11 OQ-3 resolves. |
| `SPEC_ALERT_THRESHOLD` per-mode levels | cooking=low / crisis_emergency=low / creative_focus=high / normal=medium | `companion_harness/speak_policy_config.py:93-98` | `docs/architecture-v0.1.md:476-480` | Spec-pinned by Part 6 Stage 3 YAML. |
| `SPEC_AESTHETIC_REACTION_BUDGET` per-mode rates | as spec YAML | `companion_harness/speak_policy_config.py:100-107` | `docs/architecture-v0.1.md:481-487` | Spec-pinned by Part 6 Stage 3 YAML. |
| `SPEC_EOU_POLICY.interruption_cost` | "high" | `companion_harness/speak_policy_config.py:109-113` | `docs/architecture-v0.1.md:471-473` ("never lower casually") | Spec-pinned. |

That's enough Tier A. The full audit lives next to the Tier-B allowlist (§7).

### Tier B — TUNABLE-RUNTIME (the project lead's primary surface)

Can be tuned at runtime via the dashboard. Each change emits a `config_change` event (§8). Values are spec-silent unless noted.

| Name | Default | Location | Spec source | Notes |
|---|---|---|---|---|
| **Policy thresholds** | | | | |
| `_BACKCHANNEL_THRESHOLD` | 0.7 | `companion_harness/speak_policy.py:28` | spec-silent | Above this `p_backchannel`, the EOU-confirmed action is `backchannel` instead of `full_response`. Drives `BACKCHANNEL_DETECTED` reason code. |
| `_AUDIO_VISUAL_CONFLICT_THRESHOLD` | 0.7 | `companion_harness/speak_policy.py:29` | spec-silent (`docs/architecture-v0.1.md:452-454` says behavior, not number) | Above this `audio_visual_conflict_score`, action flips to `clarification`. |
| `_GROUNDING_CONFIDENCE_THRESHOLD` | 0.5 | `companion_harness/speak_policy.py:30` | spec-silent (`docs/architecture-v0.1.md:446-450` says behavior, not number) | Below this `grounding_confidence` for a deictic reference, response is suppressed (`VISUAL_LOW_CONFIDENCE`). |
| **VAD detector** | | | | |
| `_SPEECH_THRESHOLD` | 0.5 | `companion_harness/turn_detector_vad.py:26` | spec-silent | Frame-level `p_speech` gate. Above → "in speech". |
| `_SILENCE_ONSET_MS` (VAD) | 300 | `companion_harness/turn_detector_vad.py:27` | **borderline — see §11 OQ-2** | Consecutive silence (ms) required to emit EOU. Spec lines 466–472 ("eou_policy") talk about it qualitatively but pin no number. The corresponding `physical_user_speech_onset_to_stop_ms_p95` (line 914) is Tier A; this knob is one of several inputs to that gate, but the gate is the budget, not the knob. |
| `frame_duration_ms` (VAD) | 32 | `companion_harness/turn_detector_vad.py:60` | spec-silent | Frame size in ms. Adjustable but rarely useful to tune mid-session; consider whether to expose. |
| **Smart-turn detector** | | | | |
| `_SILENCE_ONSET_MS` (SmartTurn) | 300 | `companion_harness/turn_detector_smart.py:46` | same as VAD's `_SILENCE_ONSET_MS` | Independent default — same caveat as VAD. |
| `_SILENCE_RMS_THRESHOLD` | 100 | `companion_harness/turn_detector_smart.py:47` | spec-silent | RMS energy gate for silence-candidate detection. |
| **Backchannel classifier** | | | | |
| `emit_threshold` | 0.3 | `companion_harness/backchannel_classifier.py:89` | spec-silent (manual-test-handbook.md row at line 33 names "0.3" as the operational default) | Below this `p_backchannel`, frame is suppressed (no event, no signal) to avoid drain-storm. |
| **Orchestrator** | | | | |
| `proposal_batch_window_ms` | 80 | `companion_harness/realtime_orchestrator.py:175` | spec-silent (calibration noted at line 197–198) | Grace window T4 waits for at least one ThinkerProposal before snapshot. |
| `hard_cancel_after_ms` | 120 | `companion_harness/realtime_orchestrator.py:176` | spec-silent — but feeds the Tier-A `vad_detected_user_speech_to_stop_ms_p95<200ms` budget | Time `_fire_barge_in` waits for graceful stop before forcing `cancel_generation()`. Borderline — see §11 OQ-1. |
| `p_speech_thresh` | 0.5 | `companion_harness/realtime_orchestrator.py:177` | spec-silent | Onset-detection gate for barge-in (`_should_emit_speech_onset`). |
| `p_backchannel_thresh` | 0.7 | `companion_harness/realtime_orchestrator.py:178` | spec-silent | Post-onset gate to avoid stopping playback on a backchannel (`is_barge_in_trigger`). |

(`_P_DONE = 0.05` and `_P_CONTINUE = 0.10` in `companion_harness/backchannel_classifier.py:68-69` are intentionally **not** in this table — their docstring at lines 30–39 explicitly motivates them as semantic-orthogonal constants, not threshold knobs. They are closer to Tier A even though spec-silent.)

### Tier C — TUNABLE-STARTUP

Can be tuned, but only by editing `manual_test_console/config.yaml` and restarting the server. Listed for completeness; the dashboard does **not** expose these (any attempted change must be HTTP 403'd by the §5 schema check, same path as Tier A).

| Name | Default | Location | Why startup-only |
|---|---|---|---|
| ASR `MODEL_ID` | "tiny.en" | `companion_harness/asr_faster_whisper.py:40` | Model load is ~seconds; not reload-friendly mid-session. |
| ASR `device`, `compute_type` | "cuda", "float16" | `manual_test_console/server.py:718` | Cold-start choice; reload re-creates the WhisperModel instance. |
| TTS Kokoro `model_path`, `voices_path` | b200 paths | `manual_test_console/server.py:689-690,703-704` | File-system pin; reload re-opens ONNX. |
| MiniCPM `init_vision` | False | `companion_harness/foreground_model_minicpm.py:120` | Loading the vision tower adds ~18 GB VRAM (`manual_test_console/server.py:662-664`). Toggle at startup with `--enable-vision` (`server.py:757-766`). |
| Silero VAD model | loaded via `_load_silero_vad_model` | `manual_test_console/server.py:670-672` | ONNX load on startup. |
| Pipecat Smart Turn v3 model | loaded via `_load_pipecat_smart_turn_model` | `manual_test_console/server.py:675-678` | ONNX load on startup. |
| Backchannel ASR-lexicon model | loaded via `_load_asr_lexicon_backchannel_model` | `manual_test_console/server.py:681-684` | whisper-tiny+lexicon load on startup. |
| `RETRIEVAL_TOP_K` | 5 | `companion_harness/realtime_orchestrator.py:77` | Could in principle be Tier B; classifying as Tier C because changing it mid-session would change `memory_retrieval_event` payload shape across the session boundary (audit confusion). |
| `MEMORY_EVENT_PAYLOADS_CAP` | 1024 | `companion_harness/realtime_orchestrator.py:78` | FIFO cap; changing mid-session would cause silent eviction-rate change with no operator-visible reason. |
| `_AUDIO_QUEUE_BOUND` / `_TURN_SIGNAL_QUEUE_BOUND` / `_POLICY_DECISIONS_QUEUE_BOUND` | 64 / 32 / 8 | `companion_harness/realtime_orchestrator.py:103-105` | Queue depths are construction-time; changing them after `start()` is silently no-op. |

---

## §2 — Config file design

**Format.** YAML, mirroring the style of `docs/implementation-config.yaml` and `companion_harness/replay_privacy_policy.yaml` (header comment with file purpose + spec citation, then nested keys).

**Location.** `manual_test_console/config.yaml`. Manual-test-specific; deliberately **not** in `companion_harness/` because the harness package stays SDK-pure and adapter-first (CLAUDE.md project-specific rule "Adapter-first"). Override path via env var `MANUAL_TEST_CONFIG_PATH` (resolved on server startup only; not re-read on dashboard changes).

**Structure.** Sketch — the exact spelling will be refined in Phase 1 implementation, but the shape is:

```yaml
# manual_test_console/config.yaml
#
# Manual-test runtime config. Tier B parameters only. See
# docs/design-config-and-dashboard.md §1 for the tiering rules.
#
# Tier A values (spec-frozen) MUST NOT appear here. Tier C values live in
# the startup: section (edit + restart server to take effect).

policy:
  backchannel_threshold:             0.7   # speak_policy.py:28
  audio_visual_conflict_threshold:   0.7   # speak_policy.py:29
  grounding_confidence_threshold:    0.5   # speak_policy.py:30

detectors:
  vad:
    speech_threshold:   0.5            # turn_detector_vad.py:26
    silence_onset_ms:   300            # turn_detector_vad.py:27 — see §11 OQ-2
    frame_duration_ms:  32             # turn_detector_vad.py:60
  smart_turn:
    silence_onset_ms:      300         # turn_detector_smart.py:46
    silence_rms_threshold: 100         # turn_detector_smart.py:47
  backchannel:
    emit_threshold: 0.3                # backchannel_classifier.py:89

orchestrator:
  proposal_batch_window_ms: 80         # realtime_orchestrator.py:175
  hard_cancel_after_ms:     120        # realtime_orchestrator.py:176 — see §11 OQ-1
  p_speech_thresh:          0.5        # realtime_orchestrator.py:177
  p_backchannel_thresh:     0.7        # realtime_orchestrator.py:178

# Tier C — startup-only. Edit + restart server. Dashboard does NOT expose these.
startup:
  asr:
    model_id:     "tiny.en"            # asr_faster_whisper.py:40
    device:       "cuda"
    compute_type: "float16"
  tts:
    kokoro_model_path:  "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
    kokoro_voices_path: "/raid/yid042/models/kokoro/voices.json"
  vision:
    init_vision: false                 # foreground_model_minicpm.py:120
  orchestrator:
    retrieval_top_k:            5      # realtime_orchestrator.py:77
    memory_event_payloads_cap:  1024   # realtime_orchestrator.py:78
```

The audit table in `manual_test_console/config_schema.py` (§7) is the source of truth; `config.yaml` is just an instance.

---

## §3 — Loading + override mechanism

Three layers, in priority order:

1. **Defaults from code** — the module-level constants (e.g. `_BACKCHANNEL_THRESHOLD = 0.7` in `speak_policy.py:28`). These do **not** move. They remain the in-code default so a fresh checkout still runs the same numbers it did before this design landed.
2. **`manual_test_console/config.yaml`** — overrides defaults at server startup. Missing file → silently use defaults. Missing keys → use defaults (no error). Unknown keys → log a warning and ignore (do not crash startup).
3. **Dashboard `/config/patch`** — overrides config-file values for the running session. Lost on restart (§0 N3).

**Server-global vs per-session.** Server-global state (one `ConfigStore` singleton per server process). Per-session would require routing each session's threshold settings through the orchestrator's per-pipeline factory; that's more wiring than v0.1f needs. See §11 OQ-3.

**Threshold flips and timing.** Configuration changes do **not** apply mid-utterance. They take effect on the next EOU boundary (the next iteration of `_policy_gate_task`'s `_t2_inbox.get()` loop in `companion_harness/realtime_orchestrator.py:349`). This is critical for replay (§6) — a threshold cannot change between the inputs and the decision of a single `policy_decision` event.

---

## §4 — Dashboard UI

A new panel is added to `manual_test_console/index.html`. The existing layout (header strip + two-column `<main>` with ingest rows / signal rows) stays intact; the panel is appended as a third pane or a collapsible section. Exact placement is a polish detail.

Sketch:

```
┌─────────────────────────────────────────────────────────────────┐
│ Threshold tuning (Tier B)                            [reset all]│
├─────────────────────────────────────────────────────────────────┤
│ Policy                                                          │
│   Backchannel threshold        [====●========]  0.70            │
│   AV conflict threshold        [====●========]  0.70            │
│   Grounding confidence         [===●=========]  0.50            │
│                                                                 │
│ Detectors                                                       │
│   VAD speech threshold         [===●=========]  0.50            │
│   VAD silence onset (ms)       [==●==========]  300             │
│   SmartTurn silence onset (ms) [==●==========]  300             │
│   SmartTurn silence RMS thresh [==●==========]  100             │
│   Backchannel emit threshold   [●============]  0.30            │
│                                                                 │
│ Orchestrator                                                    │
│   Proposal batch window (ms)   [=●===========]  80              │
│   Hard cancel after (ms)       [==●==========]  120             │
│   p_speech_thresh              [===●=========]  0.50            │
│   p_backchannel_thresh         [====●========]  0.70            │
│                                                                 │
│ (Tier A / Tier C parameters are not editable; see config.yaml)  │
└─────────────────────────────────────────────────────────────────┘
```

UI details:

- Each row is a `<label>` + `<input type="range">` + numeric readout.
- Slider min/max/step come from the per-key schema entry (§5 validation).
- On `input` event (live drag), no patch is sent — only the readout updates locally.
- On `change` event (release), a fetch `POST /config/patch` is sent with `{ key, value }`.
- `[reset all]` button POSTs to `/config/reset` to restore all Tier-B keys to their code-time defaults.
- Server response is mirrored back to the slider readout to confirm the new effective value (or surface an error toast if the patch was rejected — out-of-range, schema mismatch).
- A "frozen" expandable section shows Tier-A values as read-only chips, labeled "frozen by spec — see Part 8 / invariant #5". No way to edit; informational only. (Phase 1 implementation may defer the frozen section; it's optional.)

---

## §5 — Server API

New endpoints:

- `POST /config/patch` — body `{key: str, value: float|int|bool}`. Updates one Tier-B parameter.
- `POST /config/reset` — body `{}`. Restores all Tier-B params to code-time defaults; emits one `config_change` event per key that actually changed.
- `GET /config` — returns the current effective Tier-B state + schema metadata (key, default, min, max, type, location citation, current_value). The dashboard uses this on page load to render sliders.

**Validation (`manual_test_console/config_schema.py`).** Per-key schema entry:

```python
@dataclass(frozen=True)
class TierBSchemaEntry:
    key: str
    code_location: str          # "speak_policy.py:28"
    default: float | int | bool
    min: float | int | bool
    max: float | int | bool
    value_type: type            # float | int | bool
    description: str            # short, audit-table-friendly
```

The schema module exports an `ALLOWLIST: dict[str, TierBSchemaEntry]` — the same shape that drives both `/config/patch` validation and the contract test in §7.

Rejection rules:
- Key not in `ALLOWLIST` → HTTP 403, body `{"error": "key not in Tier B allowlist", "key": key, "tier": "A_or_unknown"}`. (A and C share the rejection path; the body's `tier` field distinguishes for the operator.)
- Value out of `[min, max]` → HTTP 400, body `{"error": "value out of range", "key": key, "min": ..., "max": ...}`.
- Type mismatch → HTTP 400, body `{"error": "type mismatch", "key": key, "expected": ...}`.

**Side effects of a successful patch:**

1. Update `ConfigStore` (in-memory singleton — see §3).
2. Push the new value into the live `LivePipeline` instances by re-keying the relevant adapter setter (e.g. for `_BACKCHANNEL_THRESHOLD` the policy module would expose a setter; for VAD's `speech_threshold` the `VADDetector` constructor argument becomes settable). Phase 1 implementation chooses the simplest wiring (likely: `ConfigStore.get_current(key)` is read at each EOU boundary in `_policy_gate_task`, so adapters don't need setters).
3. Emit a `config_change` Event (§8), with `caused_by` citing a synthesized `operator_action` event id (created at patch-handler entry — the inbound HTTP request itself becomes a logged Event so `caused_by[]` closes).

---

## §6 — Replay reproducibility (invariant #5)

This is the load-bearing piece. Dynamic threshold changes are dangerous if they can land between `policy_inputs_builder(...)` and `decide(...)` on the same EOU — that would break invariant #5 ("Policy-layer replay must be deterministic"; CLAUDE.md core invariant #5).

**Design.**

1. Every `config_change` Event is logged via the same `EventLogger` as everything else (`companion_harness/event_logger.py`). It carries `caused_by[]` pointing at the synthesized `operator_action` event.
2. Replay reads events in `seq_no` order. When the replayer encounters a `config_change` event, it applies the patch to its replay-time `ConfigStore` **before** consuming any subsequent `policy_decision` event.
3. The result: replay produces bit-identical `SpeakDecision`s because it sees the same threshold values at the same `seq_no` boundaries as the live session did.
4. To make this auditable, each `policy_decision` event's `payload_inline` SHOULD carry an `effective_config_hash` (a short SHA256 prefix of the Tier-B `ConfigStore` snapshot at decide-time). A verifier can recompute the hash from the preceding `config_change` chain and confirm it matches. If hashes diverge, the replayer is using stale config — a Stage 0 contract-test failure.

**Timing.** As §3 already states: thresholds change at EOU boundaries only. Concretely, `ConfigStore.get_current(key)` is called once per `_policy_gate_task` iteration, **after** `policy_inputs_builder(...)` runs but **before** `_speak_policy_decide(...)`. Between those two points there is no `await` (the determinism boundary noted in `companion_harness/realtime_orchestrator.py:454`), so no `config_change` can land.

**Phase 1 ships without replay wiring.** Phase 1 only logs `config_change` events; it does not refactor the replayer to consume them. The implication: replays of Phase-1 sessions where the operator changed a threshold mid-session will be Tier-B-non-deterministic. This is a known, scoped limitation called out in the manual-test-handbook delta that accompanies Phase 1 implementation. Phase 2 (§9) closes the gap.

---

## §7 — Spec-compliance guard

The Tier-A allowlist is enforced by `manual_test_console/config_schema.py`. Any `POST /config/patch` with a Tier-A key returns HTTP 403 (§5 rejection path).

**Contract tests (Phase 2 — see §9 split).**

- `test_tier_a_immutability`: for every parameter listed in §1 Tier A, POST to `/config/patch` with a valid-shaped payload; assert HTTP 403.
- `test_config_audit_completeness`: walk the code with an AST visitor (or a regex check, simpler) looking for module-level numeric constants in `companion_harness/turn_detector_vad.py`, `turn_detector_smart.py`, `backchannel_classifier.py`, `speak_policy.py`, `realtime_orchestrator.py`. Every such constant must either (a) appear in §1's Tier-B audit table, (b) appear in §1's Tier-A audit table, or (c) be explicitly excluded with an inline `# audit: tier-a-by-docstring` comment (the `_P_DONE` / `_P_CONTINUE` style — see `backchannel_classifier.py:68-69`). New constants without an audit classification fail the test.
- `test_config_change_event_schema`: round-trip a `config_change` event through the event schema to confirm `payload_kind`, `subject_class`, `sensitivity`, `retention_policy_id` shape (§8).

Phase 1 does **not** include these tests (it's a doc + config + dashboard PR, no contract gate). Phase 2 adds them once the replayer wiring is in place.

---

## §8 — Event log: `config_change` event

A new event type joins the existing v0.1e/v0.1f/v0.1g alphabet. Payload schema (mirroring the v0.1f `tool_progress_event` pattern in `companion_harness/v0_1f_event_schema.py`):

- `event_type:           "config_change"`
- `payload_kind:         "signal"` (it's a control-plane signal; not `memory_op`, not `model_output`)
- `subject_class:        "self"` (the system is the subject)
- `sensitivity:          "safe"` (numeric thresholds; no free text, no PII)
- `retention_policy_id:  "config_change_30d"` (new policy id — adds to `companion_harness/replay_privacy_policy.yaml`; mirrors `decision_trace_30d` rationale: 30 days is sufficient for "why did the harness behave differently after 14:32?" lookback).

Required payload fields (inlined or referenced):

- `key`                  : str — Tier-B allowlist key (matches `ALLOWLIST` from §5).
- `previous_value`       : float | int | bool — value before the patch.
- `new_value`            : float | int | bool — value after the patch.
- `applied_at_ms`        : int — `int(time.monotonic() * 1000)` at patch handler entry, mirroring the orchestrator's `now_ms` convention (`companion_harness/realtime_orchestrator.py:122`).
- `operator_action_event_id` : str — the upstream `operator_action` event id (always present in `caused_by[]`).

The `operator_action` upstream event is itself new. Schema:

- `event_type:           "operator_action"`
- `payload_kind:         "signal"`
- `subject_class:        "operator"` (new subject class — needs adding to the schema alphabet; alternative is reusing `"third_party"` if a new class is too costly. Phase 1 chooses based on what the existing alphabet allows; see §11 OQ-4.)
- `sensitivity:          "safe"`
- `retention_policy_id:  "config_change_30d"` (shares the policy with `config_change`).
- Payload: `endpoint`, `client_ip`, `request_id`. No request body content (the body is the patch, which already lives in the downstream `config_change`).

---

## §9 — Migration plan

Three phases. Phase 1 is the immediate deliverable; Phase 2 closes the replay gap; Phase 3 is speculative.

**Phase 1 — v0.1f (this design's implementation PR).**

Scope:
- Add `manual_test_console/config_schema.py` with the Tier-B `ALLOWLIST` and the rejection-side classification of Tier-A / Tier-C keys.
- Add `manual_test_console/config.yaml` with the defaults.
- Add `ConfigStore` singleton, wire it into `LivePipeline.build_live_pipeline(...)` so adapters read current values at EOU boundaries.
- Add `GET /config`, `POST /config/patch`, `POST /config/reset` endpoints to `manual_test_console/server.py`.
- Add the threshold-tuning panel to `manual_test_console/index.html`.
- Add `config_change` event type to the v0.1f event-schema module and `config_change_30d` retention policy to `replay_privacy_policy.yaml`.
- Add a new `subject_class` value (if §11 OQ-4 lands "yes new class") or reuse an existing one.

Out of scope for Phase 1:
- Replay wiring (the replayer does not yet consume `config_change`).
- The contract tests `test_tier_a_immutability` and `test_config_audit_completeness`.

Document the replay limitation in the Phase-1 PR description and in `docs/manual-test-handbook.md` (1-line update).

**Phase 2 — v0.1g (or v0.1f.1 follow-up).**

Scope:
- Refactor the replayer (`companion_harness/replay.py`) to read `config_change` events in `seq_no` order and apply them to a replay-time `ConfigStore` before consuming subsequent `policy_decision` events.
- Add `effective_config_hash` to `policy_decision.payload_inline`.
- Add the contract tests from §7 (`test_tier_a_immutability`, `test_config_audit_completeness`, `test_config_change_event_schema`).
- Decide per-session vs server-global state (§11 OQ-3).

**Phase 3 — v0.2 (speculative).**

- Per-user / per-fixture config profiles. Useful when the same b200 hosts multiple manual-test sessions in sequence and the project lead wants to "save a known-good tuning" without editing source.
- Cross-restart persistence (if §0 N3 is revisited).

---

## §10 — Out of scope

Stated explicitly so reviewers don't flag them as gaps:

- **Persisting tuned config across restarts.** Per §0 N3.
- **Tier-A overrides via any mechanism.** Per §0 N1 + §7.
- **Production-grade auth on `/config/patch`.** Per §0 N4 (manual-test rig, behind ssh tunnel).
- **Dashboard for vision parameters that aren't tunable yet.** Scene-change scoring is stubbed (`manual_test_console/server.py:115-119` `_NullSceneScorer` returns 0.0); deictic grounding is stubbed (`server.py:122-126` `_NullGroundingModel` returns ("", 0.0)). When real scoring models land, the relevant thresholds enter the Tier-B audit. Until then, no slider — exposing a slider for a parameter that doesn't drive behavior would be theater.
- **Memory parameters.** Covered separately by `docs/plan-memory-wiring-followup.md`. Memory thresholds (salience cutoffs, retrieval scores) are not added in this design; they fold in when that plan lands and we know what's tunable.
- **TTS prosody-tag interpretation.** Kokoro doesn't honor expressive tags (architecture spec Part 9 names CosyVoice2 as the prosody-aware target; we're not running it). Out of scope until prosody is real.
- **Latency-budget knobs.** The per-action latency budgets in `docs/architecture-v0.1.md:391-401` (backchannel p50<200ms, full_response p50<800ms / p95<1500ms, etc.) are targets for measurement, not runtime gates. Not tunable. Not in this design.

---

## §11 — Open questions for project-lead decision

- **OQ-1. Is `hard_cancel_after_ms` (120 ms) truly Tier B?** Borderline. The Tier-A acceptance gate at `docs/architecture-v0.1.md:913` says `vad_detected_user_speech_to_stop_ms_p95 < 200 ms`. `hard_cancel_after_ms` is one of the inputs to that gate (it's the ceiling on the graceful-stop window). If we let the operator tune it down to 50 ms, we cripple the graceful path; if we let them tune it up to 500 ms, we blow the gate. **Recommendation:** keep it Tier B for now but cap its slider range conservatively (e.g. min 50, max 180) so the gate stays achievable, and document in the slider label that exceeding ~180 will fail the spec acceptance gate.
- **OQ-2. Is `silence_onset_ms` (300 ms, VAD and SmartTurn) Tier B?** Spec lines 466–472 set qualitative expectations ("interruption_cost: high (never lower casually)") but pin no number. The corresponding Tier-A gate is the user-perceived `physical_user_speech_onset_to_stop_ms_p95` — but that's a metric, not a knob. **Recommendation:** Tier B, with the same conservative slider range as OQ-1 (e.g. min 150, max 600). The label should warn that lowering it tightens EOU at the cost of false-interruptions (which are also Tier-A gated, `false_interruption_count_per_10_min < 1`).
- **OQ-3. Per-session vs server-global state.** Phase 1 is server-global (§3). Is that fine for v0.1f, or does the project lead want per-session from the start? Per-session adds wiring to `LivePipeline` and a `session_id` axis on the dashboard; not free.
- **OQ-4. New `subject_class = "operator"` or reuse an existing class?** §8 needs a subject class for the upstream `operator_action` event. Adding a new value to the schema alphabet has a "durable metadata" cost (CLAUDE.md project-specific rules on free-text fields — also applies to closed enums per Part 5). Reusing `"third_party"` would be semantically odd (the operator is not a third party). **Recommendation:** add `"operator"` to the alphabet.
- **OQ-5. Is the Phase-1 / Phase-2 split acceptable?** Phase 1 ships threshold tuning without replay-deterministic guarantees. For manual testing this is fine (the project lead is debugging, not asserting replay invariants on those sessions). For any session intended to feed a contract test, the operator must not touch the dashboard. **Recommendation:** ship the split; document the rule in the handbook; close the gap in Phase 2.

---
