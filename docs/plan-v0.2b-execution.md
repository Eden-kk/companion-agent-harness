# Plan — v0.2b Execution (Real diarization adapter + POLICY_VERSION bump `v0.1j → v0.1k`)

## Status

**DRAFT — 2026-05-16.** Source roadmap: `docs/roadmap-v0.2-draft.md` §Wave 2 (Tasks 7–11). Not yet plan-critic'd; not yet sub-agent dispatched. This plan covers v0.2 sub-stage **v0.2b** in isolation; v0.2a (real BackgroundReasoner) and v0.2c (real benchmark data loaders) have their own execution plans.

> v0.2b lands the production-quality diarization adapter (`pyannote/speaker-diarization-3.1` primary, `3.0` ungated fallback) behind the `DiarizationAdapter` Protocol, adds `current_speaker_id` to `PolicyInputs` with a speaker-continuity tie-breaker in `derive_user_addressed_agent`, and bumps `POLICY_VERSION` from `v0.1j` to `v0.1k` (first half of the two-step cadence locked by `roadmap-v0.2-draft.md` Anchor 1). Diarization defaults OFF (`--enable-diarization` opt-in) so bit-identical Tier-B replay vs v0.1j fixtures is preserved when the flag is absent.

---

## Pinned success criterion

> **v0.2b succeeds when:** (1) `DiarizationAdapter` Protocol is defined with `process_chunk(audio_bytes, ts_mono_ms, muted: bool) -> DiarizationFrame` and a `_NullDiarizationAdapter` returning `DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)`; (2) `PyannoteDiarizationAdapter` loads on the b200 dev volume and emits `diarization_frame_produced` events with closed `caused_by[raw_audio_chunk.event_id]` causal chains; (3) `current_speaker_id: str | None` is in `PolicyInputs` with the v0.1k tie-breaker reading from a `speaker_continuity_anchor` event captured at wake-word confirmation; (4) `POLICY_VERSION == "v0.1k"` with every fixture pinning the version migrated; (5) the two adapter-readiness contract tests `test_diarization_adapter_satisfies_protocol` and `test_pyannote_loads_without_error` pass; (6) `test_policy_replay_exact` is green at the v0.1k pin across the full migrated fixture set.

The two accuracy-style gates (`diarization_speaker_continuity_addressing_accuracy > 0.85`, `diarization_false_speaker_change_rate < 0.05`) are **dropped from the ship criteria** for v0.2b and deferred to Eval Phase C when the ground-truth multi-speaker fixture pack (`live_examiner_diarized_session_001`) lands. They remain in the roadmap numeric-gates table tagged `informational` for v0.2b and become `blocking` at Phase C land.

---

## §Anchors (locked at v0.2b execution-plan ratification)

### Anchor 1 — pyannote primary `speaker-diarization-3.1` (gated); fallback `speaker-diarization-3.0` (ungated, Nov 2023)

`PyannoteDiarizationAdapter` attempts to load `pyannote/speaker-diarization-3.1` first (HuggingFace user-agreement gated). If `HF_TOKEN` is missing or the gate is unaccepted, it falls back to `pyannote/speaker-diarization-3.0` (ungated public release, November 2023). Both releases share API parity per the pyannote 3.x release notes; the adapter's streaming inference loop is unchanged across the two pins.

**Justification:** the v0.1j discipline ("no hardcoded gated-token requirement at first contact") carries forward. The 3.0 fallback lets fresh-checkout `manual_test_console` runs work without operator HF-account setup; opting into 3.1 is a deliberate `HF_TOKEN` + accept-gate action documented in `docs/remote-dev.md`.

**Replay impact:** the chosen pin is recorded in the `diarization_frame_produced` event payload (`model_revision: "pyannote/speaker-diarization-3.{0,1}"`) so replay reports surface which pin was active. Tier-B replay determinism is unaffected because the policy path consumes only the derived `speaker_id` / `confidence` numerics.

### Anchor 2 — `DiarizationAdapter.process_chunk` is the Protocol surface; mute state is a parameter, not side-channel

```python
class DiarizationAdapter(Protocol):
    def process_chunk(
        self,
        audio_bytes: bytes,
        ts_mono_ms: int,
        muted: bool,
    ) -> DiarizationFrame: ...
```

`muted` is a **per-call parameter on the Protocol**, not a setter / context-var / global. The caller (live pipeline) computes the mute-window state at chunk-dispatch time and passes it explicitly so the adapter is testable as a pure function and replay-deterministic.

**Justification:** every other v0.1 adapter Protocol (UrgencyScorer, AVConflictScorer, AddressingClassifier, DeicticDetector) is a pure callable. Introducing side-channel mute state would create an off-Protocol coupling that breaks the existing seam discipline and complicates `_NullDiarizationAdapter` (no state to mock). Per CLAUDE.md "no configurability without a 2nd use case", side-channel state has no second consumer.

**Replay impact:** `muted` is reconstructable from the event log (`assistant_audio_buffer_queued/flushed` chain) during Tier-B replay; the Protocol parameter makes the dependency explicit at the boundary.

### Anchor 3 — Mute-window: `assistant_audio_buffer_queued` → `assistant_audio_buffer_flushed + 300ms` trailing edge

The mute window opens when the live pipeline sees `assistant_audio_buffer_queued` (TTS chunk dispatched to playback) and closes 300 ms after the matching `assistant_audio_buffer_flushed` (playback drained). During the window, `process_chunk(..., muted=True)` is called; the adapter does NOT emit a `diarization_frame_produced` event for muted chunks and does NOT update the per-session speaker registry.

**Justification:** TTS audio re-captured by the room microphone is the documented Wave-2 acoustic-feedback edge case (`roadmap-v0.2-draft.md` §Risk #2). The 300 ms trailing edge covers room reverberation tail (single-room manual-test conditions). Cross-room or far-field rigs are out of scope for v0.2 (deferred to v0.3 deployment posture per Anchor 5 of roadmap).

**Replay impact:** the mute-window decision is **derived from event-log state**, not from wall-clock or external timers, so Tier-B replay reconstructs it bit-identically.

### Anchor 4 — Two new event types: `diarization_frame_produced` (per non-trivial frame) + `speaker_continuity_anchor` (at wake-word confirmation)

- `diarization_frame_produced` is emitted for every chunk that produces a non-trivial frame (i.e. `speaker_id is not None`). Muted-window chunks and unvoiced chunks do NOT emit. Causal chain: `caused_by=[raw_audio_chunk.event_id]`. Payload carries `speaker_id`, `confidence`, `is_new_speaker`, `model_revision`. Retention: `signal_default_30d` (already defined in `companion_harness/replay_privacy_policy.yaml:113`; no policy file change required).
- `speaker_continuity_anchor` is emitted **once per wake-word confirmation**, with payload `{speaker_id: str, wake_word_event_id: str}` and `caused_by=[<wake-word AddressingSignal event id>]`. This event is the durable anchor consumed by the v0.1k tie-breaker; it is not emitted per chunk.

**Justification:** every signal seam in v0.1 emits a log event (invariant #1). The two-event split mirrors the spec pattern of "continuous low-level signal events" + "discrete anchor events" used by other stages (e.g. `aesthetic_proposal_generated` vs `recent_shared_moment_referenced` in v0.1g).

**Replay impact:** the v0.1k tie-breaker reads the latest `speaker_continuity_anchor` from the event log via the PolicyInputs anchor-list (NOT via a runtime registry), so replay reconstruction is the standard event-log replay path.

### Anchor 5 — POLICY_VERSION migration `v0.1j → v0.1k` is enumerated as Task T4; fixture sweep is exhaustive

The current `POLICY_VERSION` on `main` is **`v0.1j`** (`companion_harness/speak_policy.py:19`, verified against `origin/main` 2026-05-16). Task T4 sweeps all literal occurrences and the migration is a single atomic PR (so no fixture lags the live constant). Enumerated migration sites (verified by `grep -rn "v0\.1j"` on `origin/main`):

**Production code (1):**
- `companion_harness/speak_policy.py:19` — `POLICY_VERSION = "v0.1j"` → `"v0.1k"`.

**Tests pinning the literal (3):**
- `tests/test_decision_trace_store.py:26,88,114` — three `policy_version="v0.1j"` literals.

**Tests asserting the version constant (1):**
- `tests/test_speak_policy_tool_status.py:68` — `assert speak_policy.POLICY_VERSION == "v0.1j"`. Test name is `test_policy_version_bumped_to_v0_1j` — rename to `test_policy_version_bumped_to_v0_1k` and update assertion.

**Replay-report fixtures (1 + new):**
- `tests/test_v0_1j_replay_report_script.py:40-42` — `report["policy_version"] == "v0.1j"`. Keep as-is (it is the v0.1j-pinned report; do not migrate). A new `scripts/v0_1k_replay_report.py` + `tests/test_v0_1k_replay_report_script.py` pair is created in Task T4 mirroring the v0.1j pair.

**Fixture JSON files:** zero hits for the literal `"v0.1j"` in `companion_harness/fixtures/**/*.json` on `origin/main`. Fixtures pin schema via `schema_version` / `event_type`, not policy_version. **No fixture-JSON migration is needed.** Verified by `grep -rln "v0.1j" companion_harness/fixtures/` (empty).

**`test_policy_replay_exact`** (`tests/test_policy_replay_exact.py`) — read at execution time to enumerate any internal version pins; expected to follow the `POLICY_VERSION` constant via import, but the explicit check is part of Task T4 success criteria.

### Anchor 6 — `_NullDiarizationAdapter` returns a Protocol-conformant `DiarizationFrame`, NOT a raw tuple

```python
@dataclass(frozen=True)
class DiarizationFrame:
    speaker_id: str | None
    confidence: float
    is_new_speaker: bool

class _NullDiarizationAdapter:
    def process_chunk(self, audio_bytes, ts_mono_ms, muted):
        return DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)
```

The `roadmap-v0.2-draft.md` Task 7 line uses shorthand "`returns (None, 0.0, False)`" — this plan **resolves the shorthand**: the null path returns a `DiarizationFrame` dataclass instance so the Protocol return-type is consistent across the real and null implementations. Raw tuples would break `runtime_checkable` Protocol introspection and force every caller to handle two shapes.

### Anchor 7 — Tier-3 addressing speaker-continuity tie-breaker lives in `derive_user_addressed_agent`, NOT in `MiniCPMAddressingClassifier`

The roadmap §Wave 2 Task 10 ("Extend `MiniCPMAddressingClassifier` to consume `current_speaker_id`") is **deferred from v0.2b** to a follow-up PR. Reason: `MiniCPMAddressingClassifier` is gated on issue #157 (libcudart unavailable on local dev; MiniCPM-o native duplex blocked). Until #157 resolves, the implementation path is `_NullMiniCPMAddressingClassifier` → fallback to `WakeWordAddressingClassifier`. Wiring the speaker-continuity signal into a code path that always falls back to the safety-net would mean the v0.1k tie-breaker is unobservable in any live or replay run.

**Decision:** in v0.2b, the speaker-continuity tie-breaker is added to `derive_user_addressed_agent` (in `companion_harness/addressing_classifier.py`), which sits AFTER both the MiniCPM and WakeWord classifiers in the call chain and is the canonical place where addressing tier → bool conversion happens. The tie-breaker reads `current_speaker_id` from `PolicyInputs` (set by the live builder from the latest `speaker_continuity_anchor` event) and applies the rule:

> If the implicit tier would return False AND `current_speaker_id` matches the speaker_id captured at the most recent wake-word confirmation, return True (the same speaker is still talking to us).

Migrating the tie-breaker into `MiniCPMAddressingClassifier` once #157 lands is a follow-up task on the v0.1l backlog. Until then, the tie-breaker is in effect via the canonical derivation path.

---

## §Closed open questions (resolved at v0.2b plan ratification)

- **OQ-A — Persist speaker registry across sessions?** **NO.** Cross-session speaker recognition is explicitly out-of-scope per `roadmap-v0.2-draft.md` §Out of scope ("single-session at v0.2"). The per-session embedding registry is in-memory only; session end discards it. A returning user across sessions gets a new `speaker_id`. Documented limitation surfaced in `manual-test-handbook.md` v0.2b refresh.
- **OQ-B — Maximum tracked speakers per session?** **Hardcoded constant `MAX_SPEAKERS_PER_SESSION = 4`** in `companion_harness/diarization_pyannote.py`. Per CLAUDE.md "no configurability without a second use case", we do not expose a CLI flag or ConfigStore key. The 4-speaker cap covers single-operator + small-group manual-test scenarios; multi-room / many-party rigs are out of scope.
- **OQ-C — CUDA auto-detection vs explicit `--diarization-gpu` flag?** **Auto-detect in `PyannoteDiarizationAdapter.__init__`** (`torch.cuda.is_available()`). No new CLI flag. Same posture as `tts_minicpm_native` and the v0.1h Wave 2 vision sidecar. The auto-detected device is logged on adapter init and surfaced in the replay-report adapter-stack section.
- **OQ-D — Same user across two sessions gets the same speaker_id?** **NO** — explicit consequence of OQ-A. Documented in `manual-test-handbook.md` v0.2b refresh + in the `DiarizationAdapter` Protocol docstring so future readers know not to rely on cross-session identity.
- **OQ-E — Where does the v0.1k tie-breaker live?** **`derive_user_addressed_agent`**, NOT `MiniCPMAddressingClassifier`. See Anchor 7 above.

---

## §Dependency graph

```
┌──────────────────────────────────────────────────────────────────────┐
│ Wave 1 (Protocol foundation — must land before any other v0.2b task)│
│   T1: DiarizationAdapter Protocol + DiarizationFrame dataclass      │
│       + _NullDiarizationAdapter + diarization_frame_produced +      │
│       speaker_continuity_anchor schema entries                       │
└─────────┬────────────────────────────────────────────────────────────┘
          │
   ┌──────┼──────────────────────────────┐
   │      │                              │
   ▼      ▼                              ▼
 Wave 2 (real adapter, b200)         Wave 3 (policy + addressing, local)
   T2: PyannoteDiarizationAdapter      T3: PolicyInputs.current_speaker_id +
       (streaming + embedding              speaker_continuity_anchor emission
       registry + mute-window)             at wake-word confirmation
                                       T4: POLICY_VERSION bump v0.1j→v0.1k
                                           + fixture migration sweep
                                       T5: derive_user_addressed_agent
                                           speaker-continuity tie-breaker
   │                                       │
   └──────────────┬────────────────────────┘
                  ▼
        Wave 4 (live-pipeline wiring)
          T6: --enable-diarization CLI flag in
              manual_test_console/server.py +
              live_pipeline mute-window wiring
                  │
                  ▼
        Wave 5 (contract tests + replay extension + eval-roadmap update)
          T7: 8+ contract tests
          T8: Update docs/roadmap-eval-draft.md
              Phase C prereqs (retire v0.1j Task 9 stub)
```

---

## §Concurrency map

| Wave | Concurrency | Tasks | Notes |
|---|---|---|---|
| 1 | sequential | T1 | Protocol + null + event schemas land first; T2–T8 import from here. |
| 2 | 1× (b200) | T2 | Real-adapter implementation on b200; gates on Wave 1. |
| 3 | sequential within wave | T3 → T4 → T5 | T3 adds the field; T4 bumps the version (atomic, single PR); T5 adds the rule that reads the field. Same-PR-or-strict-order to avoid intermediate-test-failure windows. |
| 4 | 1× | T6 | Live wiring; depends on Wave 2 (real adapter exists) AND Wave 3 (PolicyInputs has the field). |
| 5 | 3× parallel | T7 (test split per test), T8 (docs PR) | Contract tests can split across files; docs PR is independent. |

Wave 2 (b200 work) and Wave 3 (local-only) **CAN run in parallel** in two worktrees because they touch disjoint files; T6 (Wave 4) blocks on both being merged.

---

## §Tasks

### T1 — `DiarizationAdapter` Protocol + `_NullDiarizationAdapter` + event-schema entries

**File:** `companion_harness/diarization_adapter.py` (new).
**File:** `companion_harness/v0_1g_event_schema.py` (extend `EVENT_TYPE_SCHEMAS` dict) — or a new `v0_2_event_schema.py` if the v0.1g module's frozen-scope discipline forbids new entries (decided at plan-critic).

**Deliverable:**
- `@dataclass(frozen=True) class DiarizationFrame`: `speaker_id: str | None`, `confidence: float`, `is_new_speaker: bool`.
- `@runtime_checkable class DiarizationAdapter(Protocol)`: `process_chunk(audio_bytes: bytes, ts_mono_ms: int, muted: bool) -> DiarizationFrame`.
- `class _NullDiarizationAdapter`: implementation returning `DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)`. Tagged with `# UNAVAILABLE: <issue>` per v0.1j Task 16 discipline (file an issue at T1 land).
- Event-schema entries for `diarization_frame_produced` (payload_kind="signal", subject_class="self", sensitivity="safe", retention_policy_id="signal_default_30d") and `speaker_continuity_anchor` (payload_kind="signal", subject_class="self", sensitivity="safe", retention_policy_id="signal_default_30d").

**Success criterion:** `test_diarization_adapter_satisfies_protocol` (Wave 5 / T7) passes against `_NullDiarizationAdapter`.

### T2 — `PyannoteDiarizationAdapter` (real streaming inference)

**File:** `companion_harness/diarization_pyannote.py` (new). **Execution venue:** b200 (model weights + GPU).
**Imports:** `pyannote.audio` (try `Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=os.environ.get("HF_TOKEN"))`; on failure fall back to `pyannote/speaker-diarization-3.0`). The fallback choice is logged + recorded in the first emitted `diarization_frame_produced.payload.model_revision`.

**Deliverable:**
- `class PyannoteDiarizationAdapter` implementing `DiarizationAdapter`.
- Per-session in-memory embedding registry capped at `MAX_SPEAKERS_PER_SESSION = 4` (OQ-B).
- Mute-window logic: when `muted=True`, return `DiarizationFrame(speaker_id=None, confidence=0.0, is_new_speaker=False)` WITHOUT updating the registry and WITHOUT emitting `diarization_frame_produced`.
- Auto-detect CUDA in `__init__` (OQ-C); fall back to CPU with a `model_stack_init_summary` log entry.
- Latency budget: p95 per-chunk wall-clock < 50 ms on b200 measured at the `process_chunk` boundary (numeric gate `diarization_latency_ms_p95`).

**Success criterion:** `test_pyannote_loads_without_error` (Wave 5 / T7) passes; `tests/test_per_signal_smoke.py` extension at the diarization seam passes.

### T3 — `PolicyInputs.current_speaker_id` + `speaker_continuity_anchor` event emission

**File:** `companion_harness/schemas.py` (extend `PolicyInputs`).
**File:** `companion_harness/realtime_orchestrator.py` (emit `speaker_continuity_anchor` at wake-word confirmation site).

**Deliverable:**
- Add `current_speaker_id: str | None = None` to `PolicyInputs` (default `None` so all existing call sites continue to type-check; opt-in path sets it via live builder).
- At the orchestrator site where `AddressingClassifier` returns `confidence="explicit"` AND the diarization adapter has a non-null `speaker_id` for the same chunk, emit `speaker_continuity_anchor` with `payload={speaker_id, wake_word_event_id}` and `caused_by=[<wake-word AddressingSignal event id>]`. Idempotent within a wake-word episode (don't emit twice for the same wake-word event).

**Success criterion:** new contract test `test_speaker_continuity_anchor_emitted_at_wake_word` passes.

### T4 — POLICY_VERSION bump `v0.1j → v0.1k` + fixture migration sweep + `v0_1k_replay_report.py`

**Atomic PR** (so no fixture lags the live constant). Migration sites enumerated in Anchor 5.

**Deliverable:**
- `companion_harness/speak_policy.py:19` — `POLICY_VERSION = "v0.1k"`.
- `tests/test_decision_trace_store.py:26,88,114` — three literal swaps.
- `tests/test_speak_policy_tool_status.py:68` — assertion + test-name rename.
- New `scripts/v0_1k_replay_report.py` mirroring `scripts/v0_1j_replay_report.py` structure, with v0.1k-stage gates table additions (`diarization_latency_ms_p95`, `diarization_false_speaker_change_rate` [informational at v0.1k], `tombstone_provenance_chain_closure_rate` [carried from v0.2 roadmap]).
- New `tests/test_v0_1k_replay_report_script.py` mirroring `tests/test_v0_1j_replay_report_script.py`.
- Leave `tests/test_v0_1j_replay_report_script.py` and `scripts/v0_1j_replay_report.py` untouched — they pin the v0.1j milestone report.

**Success criterion:** `test_policy_replay_exact` passes at the new pin; all migrated tests green; `test_v0_1k_replay_report_script.py` green.

### T5 — Extend `derive_user_addressed_agent` with speaker-continuity tie-breaker

**File:** `companion_harness/addressing_classifier.py` (extend `derive_user_addressed_agent`).

**Deliverable:**
- Add a `current_speaker_id: str | None = None` parameter (last positional, default None for backward compat).
- Add a `last_anchored_speaker_id: str | None = None` parameter (read from event-log replay of the latest `speaker_continuity_anchor`, supplied by the live builder via PolicyInputs).
- Rule (applies only when the implicit tier would otherwise return False): if `current_speaker_id is not None AND current_speaker_id == last_anchored_speaker_id`, return True.
- Update the live builder in `manual_test_console/live_pipeline.py:_make_live_policy_inputs_builder` (Anchor 7) to pass both fields from the latest event-log scan.

**Success criterion:** new contract test `test_speaker_continuity_tie_breaker_flips_implicit_to_true` passes; existing `WakeWordAddressingClassifier` / `derive_user_addressed_agent` behavior unchanged when `current_speaker_id is None` (backward-compat gate).

### T6 — `--enable-diarization` CLI flag + live-pipeline mute-window wiring

**File:** `manual_test_console/server.py` (CLI flag).
**File:** `manual_test_console/live_pipeline.py` (factory wiring + mute-window dispatch).

**Deliverable:**
- `--enable-diarization` flag dispatching `PyannoteDiarizationAdapter` (default) or `_NullDiarizationAdapter` (when flag absent). Default OFF (preserves backward compat per roadmap Anchor 3).
- Live pipeline: subscribe to `assistant_audio_buffer_queued` / `assistant_audio_buffer_flushed`, compute mute-window state at chunk-dispatch time, pass `muted=` to `DiarizationAdapter.process_chunk`.
- Live builder: read latest `speaker_continuity_anchor` from event log; populate `PolicyInputs.current_speaker_id` from latest non-null `diarization_frame_produced` for current turn.

**Success criterion:** `tests/test_per_signal_smoke.py` extension at the live-loop diarization path passes; manual-test handbook §v0.2b refresh records `--enable-diarization` smoke step.

### T7 — Contract tests (8+ tests, three parallelizable test files)

**Files:**
- `tests/test_diarization_adapter_protocol.py` (Protocol-shape tests).
- `tests/test_pyannote_diarization_adapter.py` (b200-gated real-load tests).
- `tests/test_diarization_event_chain.py` (causal-DAG tests).

**Tests (≥8):**
1. `test_diarization_adapter_satisfies_protocol` — `isinstance(_NullDiarizationAdapter(), DiarizationAdapter)` (`runtime_checkable`).
2. `test_null_diarization_adapter_returns_protocol_conformant_frame` — return is a `DiarizationFrame` instance, not a raw tuple (Anchor 6).
3. `test_pyannote_loads_without_error` — b200-tagged; both pins (`3.1` if `HF_TOKEN`; `3.0` fallback otherwise) load without exception.
4. `test_diarization_events_have_caused_by` — every `diarization_frame_produced` event has `caused_by=[raw_audio_chunk.event_id]` non-empty (invariant #1 / Stage-0 DAG closure).
5. `test_diarization_mute_window_suppresses_frame_emission` — chunks dispatched with `muted=True` do NOT produce a `diarization_frame_produced` event AND do not advance the embedding registry.
6. `test_diarization_mute_window_trailing_edge_300ms` — frames after `assistant_audio_buffer_flushed` are still suppressed for 300 ms then resume.
7. `test_speaker_continuity_anchor_emitted_at_wake_word` — exactly one `speaker_continuity_anchor` per wake-word episode, with `caused_by` pointing at the wake-word addressing event.
8. `test_speaker_continuity_tie_breaker_flips_implicit_to_true` — `derive_user_addressed_agent` returns True when implicit tier would return False AND `current_speaker_id == last_anchored_speaker_id`.

Add as needed: `test_diarization_adapter_off_path_bit_identical_to_v0_1j` (regression: `--enable-diarization` absent → Tier-B replay vs v0.1j fixtures matches byte-for-byte).

**Success criterion:** all 8+ tests green locally (1–2, 4–8) and on b200 (3 and any other CUDA-gated tests).

### T8 — Update `docs/roadmap-eval-draft.md` Phase C prerequisites (retire v0.1j Task 9 stub)

**File:** `docs/roadmap-eval-draft.md` (lines 246–247).

**Deliverable:**
- Replace "Real diarization populating `social_mode` (v0.1j Task 9)" with "Real diarization populating `current_speaker_id` via `PyannoteDiarizationAdapter` (v0.2b)".
- Replace "`addressing_classified` event emission (v0.1j Task 9)" with "`addressing_classified` event emission (shipped in v0.1j Task 9, PR #182) + `speaker_continuity_anchor` event emission (v0.2b T3)".
- Add a §Cross-references line: "v0.2b — `docs/plan-v0.2b-execution.md`".

**Success criterion:** docs PR merges with no broken cross-links; `tests/test_eval_quickstart_doc_renders.py` (if it touches this file) stays green.

---

## §Numeric gates (with Measurement column)

> Numeric gates are inherited from `roadmap-v0.2-draft.md` §Wave 2 with two reclassifications per the v0.2b ship-criteria scope. Carries forward all v0.1a–v0.1j gates verbatim.

### v0.2b ship-blocking gates

| Metric | Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|---|
| `test_diarization_adapter_satisfies_protocol` | pass | T7 owner | `pytest -k test_diarization_adapter_satisfies_protocol` | 1 test | blocking |
| `test_pyannote_loads_without_error` | pass | T2 owner (b200) | `pytest -k test_pyannote_loads_without_error` on b200 | 1 test (both pins) | blocking |
| `policy_replay_exact_v0_1k` | == 1.0 | T4 owner | `pytest -k test_policy_replay_exact` across all migrated fixtures at v0.1k pin | All migrated fixtures | blocking |
| `diarization_latency_ms_p95` | < 50 ms | T2 owner | Per-chunk wall-clock at `DiarizationAdapter.process_chunk()` boundary | ≥ 1000 chunks on b200 | blocking |
| `diarization_events_caused_by_closure_rate` | == 1.0 | T7 owner | `diarization_frame_produced` events with non-empty `caused_by` / total | All emitted in T7 test runs | blocking |
| `local_ci_pass_rate` | == 1.0 | Each task owner | `pytest` pass rate on local-only contract suite per `docs/remote-dev.md` | All contract tests | blocking |
| `remote_smoke_pass_rate` | == 1.0 | T2 owner | `pytest` pass rate on b200 GPU smoke suite per `docs/remote-dev.md` | All b200-tagged smoke tests | blocking |

### v0.2b informational (deferred to Phase C ground-truth)

| Metric | Gate | Owner | Measurement method | Sample size | Gate type |
|---|---|---|---|---|---|
| `diarization_speaker_continuity_addressing_accuracy` | > 0.85 (advisory at v0.2b; blocking at Phase C) | Wave 2 + Phase C owners | Per-utterance addressing decision vs `live_examiner_diarized_session_001` ground-truth labels | All Phase C fixture utterances (does NOT exist at v0.2b land) | informational at v0.2b |
| `diarization_false_speaker_change_rate` | < 0.05 (advisory at v0.2b; blocking when `diarization_acoustic_feedback_001` fixture lands) | Wave 2 owner | Speaker-id transitions / total chunks on TTS-feedback fixture | All `diarization_acoustic_feedback_001` chunks (fixture lands in Phase C) | informational at v0.2b |

**Rationale for reclassification:** both gates require a ground-truth corpus that does not exist at v0.2b land. Holding v0.2b on a corpus the milestone itself produces is a circular ship gate. The roadmap's blocking-status for these two gates is honored at Phase C land (when `live_examiner_diarized_session_001` and `diarization_acoustic_feedback_001` exist).

---

## §Risks

1. **Pyannote 3.1 gated-token friction.** Fresh-checkout operators lack `HF_TOKEN` + accept-gate. **Mitigation:** Anchor 1 — automatic fallback to `3.0` (ungated). `docs/remote-dev.md` documents the upgrade path.
2. **POLICY_VERSION migration regression window.** Bumping the constant before migrating all fixture pins makes the test suite red for the merge interval. **Mitigation:** T4 is a single atomic PR; the migration sweep is exhaustive per Anchor 5; the literal grep is part of the PR-prep checklist.
3. **Mute-window false negatives.** Room reverberation tail > 300 ms in some rigs would cause TTS to be misclassified as new speaker. **Mitigation:** Anchor 3 — 300 ms covers single-room manual-test conditions; far-field rigs deferred to v0.3 deployment posture. Phase-C fixture `diarization_acoustic_feedback_001` is the eventual regression check.
4. **Embedding registry leak across sessions.** Session lifetime is the registry lifetime, but if the adapter outlives a session (e.g. process reuse) without explicit reset, registry carries over. **Mitigation:** explicit `reset_session()` method on `PyannoteDiarizationAdapter` called at orchestrator session-start; contract test asserts post-reset `is_new_speaker=True` for first non-trivial chunk.
5. **Tier-3 tie-breaker placement regression.** Anchor 7 places the tie-breaker in `derive_user_addressed_agent`; when issue #157 resolves and `MiniCPMAddressingClassifier` becomes the primary, the tie-breaker should not double-fire. **Mitigation:** v0.1l backlog task explicitly migrates the rule; T5 docstring warns; contract test `test_speaker_continuity_tie_breaker_does_not_double_fire_with_minicpm` lands when #157 resolves.
6. **`current_speaker_id` field-add to `PolicyInputs` cascades to fixtures.** Adding a new field with a default value should not break replay, but any fixture-deserializer that strict-checks the field set would fail. **Mitigation:** field has a `None` default; T3 includes a verification grep for any strict field-set check in `evals/` and `replay.py`.

---

## §Coordination notes

- **Two-step POLICY_VERSION cadence (`v0.1j → v0.1k → v0.2-final`).** v0.2b is the first half. The v0.2-final bump is owned by `roadmap-v0.2-draft.md` Wave 6 Task 20 and is out of scope here. All migration tooling (e.g. a `scripts/find_policy_version_pins.py` if added in T4) is reused at v0.2-final.
- **Eval Phase C entry.** This plan retires the v0.1j Task 9 stub in `roadmap-eval-draft.md`. Phase C cannot start before v0.2b lands (T8 retitles the prereq) AND the Phase C ground-truth fixture pack ships. Phase C ownership lives in `roadmap-v0.2-draft.md` Wave 4, not here.
- **b200 dev-volume budget.** `pyannote-speaker-diarization-3.1` weights are ~30 MB resident; `3.0` similar. Within the budget per `docs/remote-dev.md`; no new budget request required.
- **Manual-test handbook v0.2b refresh.** A separate docs PR (NOT bundled into the T8 PR) updates `docs/manual-test-handbook.md` with the `--enable-diarization` smoke step and the OQ-A / OQ-D documented limitation. Out of scope for this plan; tracked as a v0.2b release-followup.
- **No coupling with v0.2a (real BackgroundReasoner) or v0.2c (real benchmark loaders).** Wave 2 of `roadmap-v0.2-draft.md` lists these three sub-stages as independent and parallelizable across worktrees. This plan does not touch their files.
- **No spec amendment.** v0.2b is capability-upgrade-only per `roadmap-v0.2-draft.md` OQ-8. `docs/architecture-v0.1.md` remains FROZEN.

---

## §Cross-references

- Roadmap: `docs/roadmap-v0.2-draft.md` §Wave 2 (Tasks 7–11) + Anchor 1 (POLICY_VERSION cadence) + Anchor 3 (diarization defaults OFF).
- Predecessor execution plan: `docs/plan-v0.1j-execution.md` (the v0.1j → v0.1k bump's prior step).
- Sibling execution plans: `docs/plan-v0.2a-execution.md` (real BackgroundReasoner — separate), `docs/plan-v0.2c-execution.md` (real benchmark loaders — separate).
- Design draft (background reading): `docs/plan-real-diarization-adapter-draft.md` (referenced in `roadmap-v0.2-draft.md` §Cross-references — read for design rationale).
- Eval roadmap update: `docs/roadmap-eval-draft.md` §Phase C gating prerequisites (modified by T8).
- Spec: `docs/architecture-v0.1.md` (FROZEN — never edit). Relevant sections: invariant #1 (no unlogged behavior), invariant #5 (deterministic Tier-B replay), invariant #6 (behavioral tolerance for end-to-end replay).
- Event-schema registry pattern: `companion_harness/v0_1g_event_schema.py` (precedent for new event-type schema entries).
- Retention policy: `companion_harness/replay_privacy_policy.yaml` `signal_default_30d` entry (line 113) — reused by both new event types; NO file change required.
- Live builder Protocol signature: `manual_test_console/live_pipeline.py:_make_live_policy_inputs_builder` (`Callable[[TurnSignal, list[TurnSignal]], PolicyInputs]`) — unchanged by v0.2b (PolicyInputs gains a defaulted field; signature stable).
- Addressing classifier: `companion_harness/addressing_classifier.py` (`derive_user_addressed_agent`, the tie-breaker site per Anchor 7).
- Mute-window event source: `companion_harness/audio_output_controller.py` (`assistant_audio_buffer_queued` line 98; `assistant_audio_buffer_flushed` line 103).

---

## §Out of scope

- **Cross-session speaker recognition.** Per OQ-A and `roadmap-v0.2-draft.md` §Out of scope. Deferred to v0.1l+.
- **`MiniCPMAddressingClassifier` extension** (roadmap §Wave 2 Task 10). Per Anchor 7, deferred until issue #157 resolves.
- **Phase C live examiner.** Belongs to `roadmap-v0.2-draft.md` Wave 4 + a future `docs/plan-v0.2d-execution.md`.
- **`diarization_acoustic_feedback_001` and `live_examiner_diarized_session_001` ground-truth fixtures.** Owned by Phase C (Wave 4 Task 15b).
- **v0.2-final POLICY_VERSION bump (`v0.1k → v0.2-final`).** Owned by `roadmap-v0.2-draft.md` Wave 6 Task 20.
- **Configurability of `MAX_SPEAKERS_PER_SESSION`, mute-window trailing-edge ms, CUDA device.** Per CLAUDE.md "no configurability without a 2nd use case" — hardcoded constants until a second consumer appears.
- **Production deployment posture** (Docker, systemd, log shipping). Roadmap Anchor 5 — separate v0.3 scope.
- **Manual-test handbook v0.2b refresh.** Separate docs PR per §Coordination notes.
