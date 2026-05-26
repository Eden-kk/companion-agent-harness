# Plan — two-MiniCPM-o debate harness

Source design: `docs/design-two-minicpm-debate.md` (mirrored from `Eden-kk/llm-wiki` at `multi_modality_ai_assistant/thinking/two-minicpm-debate-duplex-design.md`, 2026-05-26 revision). All §-references below cite that doc.

## Goal (success criterion)

Run two MiniCPM-o-4.5 instances on a lockstep 1 Hz clock, debating one fixed motion, with these invariants holding for at least one full 30-tick debate:

1. **Single-speaker invariant.** At every tick `t`, the set `audible = {n : not r[n]["is_listen"]}` has size ≤ 1. (Collisions are resolved by arming `break_event` on the incumbent in the collision tick; `break_event` fires in the next tick. The collision tick itself may have `|audible|==2` — that is the only allowed exception. The collision must resolve within K_GRACE+2 = 3 ticks total — K_GRACE collision ticks where the incumbent may self-yield, plus 1 arm tick, plus 1 break-fire tick. The Stage 2 livelock test gate `max_collision_duration_ticks <= K_GRACE + 1 = 2` measures pre-fire collision duration only (the contiguous |audible|==2 run).)
2. **Barge-in is real.** At least one tick where the challenger transitions `listen → speak` while the incumbent was speaking the prior tick AND the incumbent is force-listened on the following tick (`break_event` armed → fired → cleared).
3. **Two artifacts saved** after the debate ends (clean stop or T_MAX):
   - `artifacts/debate/<run_id>/transcript.json` — full text trajectory (per-tick records: tick, speaker, is_listen, end_of_turn, text, audible, floor, break_armed).
   - `artifacts/debate/<run_id>/debate.wav` — single mono 24 kHz WAV rendered offline from the **text trajectory** by Kokoro TTS, with one Kokoro voice per debater, utterances stitched in chronological speak order.

"Text mode during the debate" is interpreted concretely as: the MiniCPM duplex loop still produces per-chunk audio (`generate_audio=True`) because cross-feeding the opponent's audio is what drives the LS gate and makes barge-in observable — but **none of that per-chunk MiniCPM audio is the user-facing artifact**. The user-facing audio is the offline Kokoro re-render of the text trajectory. The per-chunk MiniCPM audio is internal plumbing only and is not saved to disk by default (optional flag for debugging).

## Non-goals (for this PR series)

- No vision / no image frames.
- No web UI / no Cloudflare tunnel.
- No content judge LLM (design §5 last bullet) — recorded as a follow-up.
- No realtime mic / no realtime speaker — fully simulated, faster-than-realtime allowed.
- No integration with `realtime_loop.py` or `continuous_orchestrator.py` — this is a standalone harness in `companion_harness/debate/` that imports the existing MiniCPM model load helper but uses its own lockstep loop.

## Architectural placement

```
companion_harness/
  debate/
    __init__.py
    minicpm_duplex_session.py    # NEW — thin per-session wrapper exposing native break/force-listen
    debate_orchestrator.py        # NEW — the 1Hz lockstep loop (design §3)
    debate_artifacts.py           # NEW — transcript JSON writer + Kokoro stitcher
    prompts.py                    # NEW — system prompt template (design §4) + motion fixture
scripts/
  run_two_minicpm_debate.py       # NEW — CLI entry; loads 2 instances, runs, writes artifacts
tests/
  test_debate_break_lifecycle.py  # NEW — design §6 unit test 1 (fake duplex)
  test_debate_acoustic_overlap.py # NEW — unit test 2
  test_debate_clean_handover.py   # NEW — unit test 3 (clean handover; Stage 3 may rename when self_yields semantics tighten)
  test_debate_livelock.py         # NEW — unit test 4
  test_debate_deadlock.py         # NEW — unit test 5
  test_debate_tmax.py             # NEW — unit test 6
  test_debate_artifacts.py        # NEW — transcript schema + kokoro stitch (gpu-marker-gated)
docs/
  design-two-minicpm-debate.md    # NEW — mirror of fetched design doc (frozen reference)
  plan-two-minicpm-debate.md      # THIS
```

The new `debate/` package does NOT touch `speak_policy.py`, `realtime_orchestrator.py`, `continuous_orchestrator.py`, `event_logger.py`, or any v0.1a invariant surface. The CLAUDE.md "no unlogged behavior" / "policy determinism" invariants apply to the assistant; this debate harness is a separate research instrument and is documented as such in its module docstring. It DOES still keep its own per-tick JSONL audit (transcript.json) so every utterance is traceable, satisfying the spirit of invariant #1.

## Contracts (the seams between stages)

### C1. `MiniCPMDuplexSession` (new)

Module constant: `CHUNK_SAMPLES: int = 16000  # 16 kHz × 1 s`.

A single duplex session bound to one loaded MiniCPM-o model. Holds the native duplex handle and exposes only what the orchestrator needs.

```python
class MiniCPMDuplexSession:
    def __init__(self, base_model, *, prefix_system_prompt: str, ref_audio: np.ndarray | None,
                 force_listen_count: int = 0, name: str): ...
    def prefill(self, audio_1s: np.ndarray, *, text_list: list[str] | None = None) -> None: ...      # one tick of audio
    # text_list is forwarded to streaming_prefill; used only under Plan B (silence audio + text side-channel).
    def generate(self, *, listen_prob_scale: float) -> DuplexTickResult: ...
    def set_break(self) -> None: ...
    def clear_break(self) -> None: ...
    def is_break_set(self) -> bool: ...

@dataclass
class DuplexTickResult:
    is_listen: bool
    text: str                     # "" when is_listen=True (or break-forced silence)
    audio_waveform: np.ndarray    # always length CHUNK_SAMPLES; zeros when silent
    end_of_turn: bool
    current_time: float    # monotonic seconds from `time.monotonic()` captured at generate-call return (used only for trace/replay timestamps; not a metric input)
```

Implementation: construct via `base.as_duplex(generate_audio=True, sliding_window_mode=..., chunk_ms=1000)`; call `prepare(prefix_system_prompt=...)`; `ref_audio` is passed through whatever channel `MiniCPMODuplex` actually accepts — Stage 0 confirms by reading `modeling_minicpmo.py` `prepare()` signature; if `ref_audio` is not a `prepare()` kwarg, it is set on the duplex object before prepare via the same path the existing adapter would use (TBD at Stage 0). Pass `force_listen_count` at construction; forward `set_break_event/clear_break_event/is_break_set` directly. **Two instances of `MiniCPMDuplexSession` = two `base.as_duplex()` calls on (possibly) the same loaded `base_model`** — Stage 0 probe confirms whether the underlying weights can hold two independent duplex sessions or whether we need two `from_pretrained` loads (~2×9 GB VRAM).

### C2. `DebateOrchestrator` (new)

```python
class DebateOrchestrator:
    def __init__(self, motion: str, sessions: dict[str, MiniCPMDuplexSession],
                 *, listen_prob_scale: dict[str, float],
                 k_grace: int = 1, n_deadlock: int = 4, t_max: int = 30,
                 max_ticks: int = 60,
                 moderator_seed_audio: np.ndarray,
                 moderator_nudge_audio: np.ndarray,
                 rng_seed: int = 0,
                 plan_b_text_supplier: "Callable[[dict[str, DuplexTickResult], str], str | None] | None" = None): ...
    def run(self) -> DebateTrace: ...      # synchronous; returns full per-tick trace
    # wiring: sessions['A'].generate(listen_prob_scale=listen_prob_scale['A']) and likewise for B at each tick.
# `plan_b_text_supplier` is None under default (Plan A) and provides per-tick text-injection under Plan B; see Plan B contract subsection.

@dataclass
class TickRecord:
    tick: int
    audible: list[str]
    floor: str | None
    break_fired_this_tick: list[str]           # who the break FIRED on this tick (forced is_listen=True at generate time)
    break_armed_for_next_tick: list[str]       # who set_break_event() was called on during arbitrate, to fire on the next tick's generate
    silence_run: int
    moderator_nudge_fired: bool
    per_speaker: dict[str, dict]               # is_listen, text, end_of_turn, current_time

@dataclass
class DebateMetrics:
    total_ticks: int
    turn_count_per_speaker: dict[str, int]
    barge_in_attempts: int
    barge_in_successes: int
    collision_count: int
    max_collision_duration_ticks: int
    harness_forced_breaks: int
    self_yields: int
    deadlocks_detected: int
    moderator_nudges_fired: int
    t_max_hits: int
    # All fields derived from per-tick TickRecord data; no separate accumulator state.

# Stage 5 smoke checker asserts: barge_in_successes >= 1,
# max_collision_duration_ticks <= K_GRACE + 1, total_ticks > 0.
# If the 30-tick run produces barge_in_successes == 0, log a warning and rerun once
# with listen_prob_scale = 0.7 for both debaters before asserting failure.
# Two runs is the budget; a second zero is a hard fail.

@dataclass
class DebateTrace:
    motion: str
    ticks: list[TickRecord]
    metrics: DebateMetrics
    k_grace: int       # K_GRACE value used for this run (enables `assert metrics.max_collision_duration_ticks <= k_grace + 1` without hardcoding)
    t_max: int         # T_MAX value used for this run
    n_deadlock: int    # N_DEADLOCK value used for this run
```

Loop body is the exact §3 sequence (perceive → generate → arbitrate → plumb), with the bug-fix details preserved:
- `break_event` is set in tick `t`'s arbitrate step, held through tick `t+1`'s prefill+generate, then cleared.
- Audio is always cross-fed — `inbox[n] = sum(emitted[o] for o != n)`. Floor never gates audio.
- `K_GRACE=1` lets the incumbent perceive the challenger before being broken.
- **Floor handover on collision:** upon `overlap[ch] > K_GRACE`, the orchestrator BOTH arms the break on the incumbent AND immediately sets `floor = ch` in the SAME tick (matches design §3 pseudocode line 145). The per-tick log for the collision-resolution tick therefore shows the challenger as the new floor holder. `barge_in_successes` is counted in the FIRST tick where the armed break has actually FIRED — i.e., the tick where the prior incumbent's generate returns is_listen=True (forced) AND |audible|==1 AND audible[0]==ch AND floor==ch. This is the tick immediately following the collision-resolution tick (collision tick + 1).

### C3. `DebateArtifacts` (new)

```python
def write_transcript(trace: DebateTrace, out_path: Path) -> None: ...   # JSON
def render_audio_from_transcript(trace: DebateTrace, *,
                                  kokoro_pipeline,   # raw Kokoro pipeline OR thin sync wrapper
                                  voice_for: dict[str, str],
                                  out_wav: Path) -> None:
    # 1. Group ticks into contiguous speak-runs per speaker (turn boundaries = floor change OR end_of_turn).
    # 2. Concatenate text within each run, render each run via the sync path:
    #    samples, sr = kokoro_pipeline.create(text, voice=voice_for[speaker], speed=1.0, lang="en-us")
    #    # tuple[NDArray[np.float32], int]; sr is 24000
    #    assert sr == 24000
    # 3. Stitch all runs in chronological order; write 24 kHz mono int16 WAV.
```

`kokoro_pipeline` accepts either:
- the raw `Kokoro(model_path, voices_path)` object (exposes `.create(text, voice, speed, lang) -> tuple[NDArray[np.float32], int]`), or
- a thin sync wrapper. If reusing `KokoroTtsAdapter`, expose a `create_sync(text, voice) -> tuple[np.ndarray, int]` accessor on it; otherwise pass the raw pipeline directly.

Stage 4 deliverable must record which path was taken (raw pipeline vs. accessor).

Voice assignment: A → one Kokoro voice (e.g., `af_heart`), B → a different one (e.g., `am_michael`). Voices listed in `prompts.py`.

## Plan B contract (engages only if Stage 0.b fails)

If `generate_audio=True` throws an ImportError on this host (libcudart mismatch), the entire pipeline switches to Plan B. The binding contract for Stages 1–5 is:

- Construction stays the same except `generate_audio=False`.
- `MiniCPMDuplexSession.generate()` returns `audio_waveform = np.zeros(CHUNK_SAMPLES, np.float32)` always (zero-length emitted audio).
- Cross-feed: opponent's inbox = silence every tick (no `audio_waveform` to forward).
- Side-channel text injection: at each tick, before `prefill`, the orchestrator concatenates the opponent's last 1–3 ticks of speak text into a brief textual seed that is fed in via the confirmed call: `duplex.streaming_prefill(audio_waveform=silence_1s, text_list=[opponent_text_seed])`. `MiniCPMDuplexSession.prefill()` must accept an optional `text_list: list[str] | None = None` kwarg and forward it to `streaming_prefill`. A one-off `prepare()` re-call with rolling history is NOT allowed — fail loudly.
- All other invariants (break_event lifecycle, K_GRACE, T_MAX, deadlock nudge) are unchanged.
- Stage 5 smoke checker is the same; the single-speaker and barge-in invariants are still measurable since they read from `is_listen` and `end_of_turn`, not from audio.
- Plan B is a degraded mode — barge-in semantic latency is the same (1 tick), but the model loses the audio-domain perception of the opponent. A Plan B run is still useful for orchestrator-mechanism validation, not for cross-distribution speech behavior.

## Stages

| # | Stage | Deliverable | Gate (passing test) |
|---|-------|-------------|---------------------|
| 0 | **Probe — does base model hold two duplex sessions?** | `scripts/probe_minicpm_dual_duplex.py`: load one MiniCPM-o, call `as_duplex()` twice, run 3 ticks on each, verify both return coherent text/`is_listen`. Sub-steps: 0.a: confirm single-load-vs-two-loads verdict; 0.b: verify `base.as_duplex(generate_audio=True)` constructs without ImportError on this host — if it crashes on `libcudart.so.13`, switch the entire pipeline to Plan B (contract above) and proceed; Stages 1–5 are unchanged in their interfaces, only `MiniCPMDuplexSession`'s construction kwargs change; 0.c: read `modeling_minicpmo.py` `prepare()` signature and document the `ref_audio` plumbing path. | Probe script runs without crash on b200; reports either "single-model dual-session OK" or "needs two loads, ~2×9 GB". Result documented in plan revision log. |
| 1 | **`MiniCPMDuplexSession` adapter** + 1 unit test (break lifecycle, design §6 test 1, fake duplex) | New `companion_harness/debate/minicpm_duplex_session.py`; `tests/test_debate_break_lifecycle.py`. | `pytest -q tests/test_debate_break_lifecycle.py` passes in canonical venv. No GPU required (fake duplex). |
| 2 | **`DebateOrchestrator` lockstep loop** + 4 unit tests (acoustic overlap, self-yield, livelock, deadlock — design §6 tests 2–5) | `companion_harness/debate/debate_orchestrator.py` + tests 2–5 with fake duplex. Livelock test additionally asserts `max_collision_duration_ticks <= K_GRACE + 1` in the trace. | All 4 tests pass in canonical venv. |
| 3 | **T_MAX backstop + tick logging fields** + test 6 | Extension to orchestrator + `tests/test_debate_tmax.py`. | Test 6 passes; tick records carry all fields in C2 schema. |
| 4 | **Artifacts: transcript JSON + Kokoro stitcher** + schema test | `companion_harness/debate/debate_artifacts.py`; `tests/test_debate_artifacts.py` (uses real Kokoro, gated unconditionally by `@pytest.mark.gpu` so CI on non-b200 skips it cleanly; local b200 runs it explicitly via `pytest -m gpu`). | Test produces a small 2-utterance WAV that loads with `wave.open()` and has expected duration; transcript JSON validates against a tiny inline schema. |
| 5 | **CLI driver + end-to-end run on b200** | `scripts/run_two_minicpm_debate.py --motion "..." --max-ticks 30 --out-dir artifacts/debate/<ts>/ --load-mode {single,dual}`. CLI flag `--load-mode` controls whether one MiniCPM-o is loaded and `.as_duplex()` called twice (`single`), or two are loaded with one `.as_duplex()` each (`dual`). Default is set by Stage 0's recorded probe result: if Stage 0 found single-load OK, default is `single`; otherwise `dual`. The smoke checker logs which mode was used. | Driver runs against real two-instance MiniCPM; produces `transcript.json` and `debate.wav`; smoke checker script asserts the three success-criterion invariants. |

Stages are strictly sequential — each gates the next. Stage 0 must complete before Stage 1 begins: Stage 0 determines (a) one-load-vs-two-loads, (b) `generate_audio=True` host-availability, and (c) the `prepare()` `ref_audio` plumbing path — all of which affect C1's concrete implementation even though C1's public interface is stable.

## Risks & mitigations

- **Stage 0 says we need two model loads (~2×9 GB).** B200 has 80 GB; budget is fine. No mitigation needed beyond documenting VRAM.
- **`generate_audio=True` host-availability risk.** `tts_kokoro.py` documents that `as_duplex(generate_audio=True)` requires `stepaudio2 → torchaudio 2.11 → libcudart.so.13`, which is NOT present on this host (only `libcudart.so.12` installed, CUDA 12.8). Stage 0 sub-step 0.b verifies construction without ImportError; if it crashes, the entire PR series FAILs FAST and pivots to Plan B (text-bridge crossfeed: feed silence both directions, inject opponent's most recent text into a side-channel system message — design only on trigger, not implemented up front). Latency concern is secondary: if `=True` works but per-tick latency exceeds 30 s, document and continue — offline is fine.
- **Self-voice cross-distribution shift (design §6.1).** Stage 5 runs a self-voice check before the full debate: 3 ticks of A speaking with B receiving A's emitted audio. If any of B's 3 `generate()` responses returns `is_listen=False` AND `text` contains a non-recognizable token sequence (>50% non-ASCII or empty text fragments), declare "voice-distribution-shift detected", STOP the run, and dump a diagnostic (the 3 audio chunks + the 3 text outputs saved to `artifacts/debate/<run_id>/diagnostic/`). Operator decides whether to retry with different ref voices or trigger Plan B. **Plan B (deferred unless triggered, not implemented up front):** cross-feed silence + inject opponent's most recent `end_of_turn` text into a side-channel system message prepended to next prefill. Design Plan B only on trigger.
- **`prepare(ref_audio=...)` API mismatch.** Existing adapter uses `prefix_system_prompt=`, doesn't pass `ref_audio`. Stage 1 confirms `ref_audio` kwarg name from `modeling_minicpmo.py` source; if it's named differently, update C1 implementation only — interface unchanged.
- **`force_listen_count` placement.** It's an `__init__` param on the duplex object, not a per-call kwarg. C1 takes it in `__init__`. For the debate we'll set it to 0 (we don't want a startup forced-listen window; the moderator seed plays the role of the opener).
- **`set_break_event` for both models at once during a collision.** Design §3 breaks only the incumbent. Implementation matches: the challenger is allowed to speak; only the incumbent gets `arm_break`.

## What this PR series does NOT do

Recording these so reviewers don't ask:
- No per-debater fine-tune of `listen_prob_scale`. Both start at 0.9 (design §3 default). Tuning is a follow-up.
- No content judge LLM. Follow-up.
- No streaming Kokoro render. Offline one-shot per turn is enough for an artifact.
- No replay tool. The transcript JSON is the replay artifact.
- No PR opened to `main`. Work lands on the worktree branch; user decides whether to PR.

## Open questions to resolve during execution (not blocking)

- Exact name of the ref-audio kwarg on `MiniCPMODuplex.prepare` (Stage 1 confirms by reading source).
- Whether `generate_audio=True` returns the audio as np.ndarray, torch.Tensor, or PCM int16 (Stage 1 normalizes to np.float32 length-16000 inside `MiniCPMDuplexSession.generate`).
- Whether the moderator seed should be a Kokoro render of the opening line (preferred) or pre-recorded silence + a one-line text prompt injection. Default: Kokoro render at 24 kHz mono, then resampled to 16 kHz mono float32 via `librosa.resample` or `scipy.signal.resample_poly` before placing into `inbox`. A `_resample_24k_to_16k(audio: np.ndarray) -> np.ndarray` helper is added to `debate_artifacts.py` (used by moderator-seed generation and any debug audio dump — NOT used by `render_audio_from_transcript`, whose artifact WAV stays at 24 kHz throughout).

## Revision log

- 2026-05-26 — initial draft.
- 2026-05-26 — Stage 0 probe verdict: load_mode=single, plan_b_engaged=false, ref_audio_path=prepare_kwarg.
