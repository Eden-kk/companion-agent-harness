# Subplan — Stage 2: `DebateOrchestrator` lockstep loop + 4 fake-driven unit tests

Parent: [plan-two-minicpm-debate.md](plan-two-minicpm-debate.md). All §-refs cite [design-two-minicpm-debate.md](design-two-minicpm-debate.md).

## Scope

Implement `companion_harness/debate/debate_orchestrator.py` and the four fake-driven unit tests (`test_debate_acoustic_overlap.py`, `test_debate_self_yield.py`, `test_debate_livelock.py`, `test_debate_deadlock.py`). This subplan does NOT cover T_MAX (Stage 3), artifacts (Stage 4), or the real-model run (Stage 5).

Out-of-scope here: anything other than the orchestrator + four tests. No CLI, no artifact writer, no Kokoro, no MiniCPM real loads.

## Inputs (assumed delivered by Stage 1)

**If Stage 1 has not shipped before Stage 2 executes** (e.g., autonomous execution skipped it), this subplan also creates a stub `companion_harness/debate/__init__.py` plus a stub `minicpm_duplex_session.py` that exports `MiniCPMDuplexSession` (raises NotImplementedError in `__init__` if called with a non-fake `base_model`), `DuplexTickResult`, and `CHUNK_SAMPLES = 16000`. The orchestrator only ever sees the C1 interface, so a stub-only Stage 1 is sufficient for Stage 2's fake-driven tests. Stage 1's real implementation can land later without re-touching Stage 2.

- `companion_harness/debate/minicpm_duplex_session.py` exports:
  - `MiniCPMDuplexSession` with the C1 interface (`prefill`, `generate`, `set_break`, `clear_break`, `is_break_set`).
  - `DuplexTickResult` dataclass (`is_listen`, `text`, `audio_waveform` length `CHUNK_SAMPLES`, `end_of_turn`, `current_time`).
- `CHUNK_SAMPLES = 16000` constant available for import.

Stage 1 also delivers a `FakeDuplexSession` test helper (or equivalent) so the orchestrator tests don't depend on real MiniCPM. If Stage 1 didn't ship it, this subplan adds it as `tests/_debate_fakes.py`.

## Deliverables

1. `companion_harness/debate/debate_orchestrator.py` — `DebateOrchestrator` class implementing the loop body from design §3.
2. `tests/_debate_fakes.py` — shared `ScriptedDuplexSession` fake that returns scripted `DuplexTickResult`s and records `set_break/clear_break` calls.
3. `tests/test_debate_acoustic_overlap.py` — unit test 2 (acoustic overlap).
4. `tests/test_debate_clean_handover.py` — unit test 3 (self-yield / clean handover).
5. `tests/test_debate_livelock.py` — unit test 4 (livelock, includes the `max_collision_duration_ticks <= K_GRACE + 1` assertion).
6. `tests/test_debate_deadlock.py` — unit test 5 (deadlock + moderator nudge inserted into inbox).

## Implementation plan (the loop body)

The orchestrator's `run()` method is a single synchronous for-loop over `range(max_ticks)`. Inside each tick, the four phases of design §3 (perceive → generate → arbitrate → plumb) are executed in order. State variables live as attributes on the orchestrator instance.

### State on `DebateOrchestrator`

`__init__` signature includes `motion: str` as the first positional parameter (before `sessions`). `self._motion = motion` is set in `__init__`. In `run()`, `DebateTrace` is constructed with `motion=self._motion`. `__init__` also accepts `plan_b_text_supplier` (kw-only, default None). When non-None, `_tick`'s phase-1 prefill calls `text_seed = plan_b_text_supplier({...prior results}, name); session.prefill(audio, text_list=[text_seed] if text_seed else None)`. With None (default), `text_list` is not passed — preserves Stage 2 test behavior unchanged.

```python
self._motion: str                                  # debate motion string passed to __init__
self._sessions: dict[str, MiniCPMDuplexSession]   # {"A": ..., "B": ...}
self._listen_prob_scale: dict[str, float]
self._k_grace: int
self._n_deadlock: int
self._t_max: int                                  # used by Stage 3, parameter present here
self._max_ticks: int
self._moderator_seed_audio: np.ndarray            # length CHUNK_SAMPLES
self._moderator_nudge_audio: np.ndarray
self._rng: random.Random                          # seeded by rng_seed for tie_breaker
self._plan_b_text_supplier: Callable[[dict[str, DuplexTickResult], str], str | None] | None  # None under Plan A; wired by Stage 5 under Plan B

# Per-run mutable state (reset at top of run())
self._inbox: dict[str, np.ndarray]                # init at seed audio
self._floor: str | None                            # None at start
self._turn_len: dict[str, int]                    # {A: 0, B: 0}
self._overlap: dict[str, int]                     # {A: 0, B: 0}
self._silence_run: int                             # 0
self._break_armed: set[str]                        # empty; populated by arm_break, drained after fire
self._ticks: list[TickRecord]                      # accumulated trace
self._last_results: dict[str, DuplexTickResult]   # empty dict pre-tick-0; populated at end of each tick for next-tick supplier callbacks
```

### Per-tick algorithm (exact order)

```
def _tick(self, t: int) -> None:
    # PHASE 1: PERCEIVE — both sessions ingest opponent audio from inbox.
    # Note: a session with break_armed will internally no-op prefill (modeling guard);
    # we still call it unconditionally — the model handles the guard.
    for name, sess in self._sessions.items():
        if self._plan_b_text_supplier is not None:
            text_seed = self._plan_b_text_supplier(self._last_results, name)
            sess.prefill(self._inbox[name], text_list=[text_seed] if text_seed else None)
        else:
            sess.prefill(self._inbox[name])

    # PHASE 2: GENERATE — one chunk each. break_armed sessions return forced listen+silence+eot.
    results: dict[str, DuplexTickResult] = {}
    for name, sess in self._sessions.items():
        results[name] = sess.generate(listen_prob_scale=self._listen_prob_scale[name])

    # Record who was force-listened THIS tick, then clear the break for next tick.
    fired = set(self._break_armed)
    for name in fired:
        self._sessions[name].clear_break()
    self._break_armed.clear()

    spoke   = {n: not r.is_listen for n, r in results.items()}
    audible = sorted(n for n in spoke if spoke[n])

    # PHASE 3: ARBITRATE
    moderator_nudge_fired = False

    if not audible:
        # Both silent.
        self._silence_run += 1
        if self._silence_run >= self._n_deadlock:
            # Inject moderator nudge into NEXT tick's inbox (done at PLUMB).
            # Set a flag so PLUMB knows to use the nudge.
            moderator_nudge_fired = True
            self._silence_run = 0
    elif len(audible) == 1:
        s = audible[0]
        self._silence_run = 0
        # Self-yield detection: floor was held by other, no break armed, this speaker took over cleanly.
        # (Counted at metrics-derivation time, not here.)
        if self._floor != s:
            self._turn_len = {"A": 0, "B": 0}
        self._turn_len[s] += 1
        # end_of_turn releases the policy floor.
        self._floor = None if results[s].end_of_turn else s
        self._overlap = {"A": 0, "B": 0}
    else:
        # Collision: both audible.
        self._silence_run = 0
        if self._floor is None:
            # Seed the floor with the tie-breaker (random, NOT "A wins").
            self._floor = self._tie_breaker(t)
        challenger = "B" if self._floor == "A" else "A"
        self._overlap[challenger] += 1
        self._turn_len[self._floor] += 1
        if self._overlap[challenger] > self._k_grace:
            # Arm the break AND move the floor in the SAME tick (design §3 line 145).
            self._arm_break(self._floor)
            self._floor = challenger

    # T_MAX backstop (Stage 3 wires this; for Stage 2 it's an optional block guarded by `if self._t_max:`).
    if self._t_max and self._floor and spoke.get(self._floor) and self._turn_len[self._floor] >= self._t_max:
        self._arm_break(self._floor)
        self._turn_len[self._floor] = 0

    # PHASE 4: PLUMB — cross-feed audio; opponent's emitted audio becomes my next inbox.
    emitted = {n: (self._one_sec(results[n].audio_waveform) if spoke[n] else SILENCE) for n in results}
    next_inbox = {n: sum((emitted[o] for o in results if o != n), SILENCE.copy()) for n in results}
    if moderator_nudge_fired:
        # Replace both inboxes with the nudge audio.
        next_inbox = {n: self._moderator_nudge_audio.copy() for n in results}
    self._inbox = next_inbox

    # Update last_results for next-tick plan_b_text_supplier callbacks.
    self._last_results = results

    # Record the tick.
    self._ticks.append(TickRecord(
        tick=t,
        audible=audible,
        floor=self._floor,
        break_fired_this_tick=sorted(fired),
        break_armed_for_next_tick=sorted(self._break_armed),
        silence_run=self._silence_run,
        moderator_nudge_fired=moderator_nudge_fired,
        per_speaker={n: {"is_listen": r.is_listen, "text": r.text,
                          "end_of_turn": r.end_of_turn, "current_time": r.current_time}
                      for n, r in results.items()},
    ))

def _arm_break(self, name: str) -> None:
    if name not in self._break_armed:
        self._sessions[name].set_break()
        self._break_armed.add(name)

def _tie_breaker(self, t: int) -> str:
    return self._rng.choice(["A", "B"])

@staticmethod
def _one_sec(x: np.ndarray) -> np.ndarray:
    # Pad/truncate to exactly CHUNK_SAMPLES (defensive — sessions should already do this).
    if x is None:
        return np.zeros(CHUNK_SAMPLES, np.float32)
    x = np.asarray(x, np.float32)
    if len(x) < CHUNK_SAMPLES:
        return np.pad(x, (0, CHUNK_SAMPLES - len(x)))
    return x[:CHUNK_SAMPLES]
```

`SILENCE = np.zeros(CHUNK_SAMPLES, np.float32)` is a module-level constant.

### Metrics derivation (deferred to Stage 3's final form; Stage 2 implements the minimum)

`DebateMetrics` is derived in `_compute_metrics(ticks: list[TickRecord]) -> DebateMetrics` at the END of `run()`. For Stage 2 the orchestrator returns `DebateTrace(motion, ticks, metrics, k_grace, t_max, n_deadlock)`. The four Stage 2 tests only need these metric fields:

- `total_ticks` — `len(ticks)`.
- `collision_count` — number of ticks where `len(audible) >= 2`.
- `max_collision_duration_ticks` — length of the longest contiguous run of ticks with `len(audible) >= 2`.
- `harness_forced_breaks` — number of distinct (tick, name) pairs in `break_fired_this_tick` across all ticks.
- `moderator_nudges_fired` — number of ticks with `moderator_nudge_fired=True`.
- `self_yields` — at this stage: number of ticks where the previous tick had `audible=[other]`, this tick has `audible=[s]` and `s != other`, AND `name not in break_fired_this_tick`. Skip i=0 (no previous tick); iterate `for i in range(1, len(ticks))`. (Approximation; Stage 3 refines.)
- `deadlocks_detected` — number of ticks where `moderator_nudge_fired=True` (same count for now).
- `barge_in_attempts` / `barge_in_successes` / `turn_count_per_speaker` / `t_max_hits` — Stage 3 wires.

For Stage 2, the four tests only need the fields explicitly listed above; fields not yet derived may return 0 / empty dict from `_compute_metrics`. Stage 3 fills the rest.

## Test specs (each with a one-paragraph setup + one assertion block)

### Test 2: `test_debate_acoustic_overlap.py`

Setup: 2 scripted sessions. Force both to speak (return `is_listen=False`, `audio_waveform = np.ones(CHUNK_SAMPLES)` for A, `np.full(CHUNK_SAMPLES, 0.5)` for B) for exactly one tick (`t=0`), then both go listen for `t=1`. Run with `max_ticks=2`, `k_grace=1`.

Assertion: at tick `t=1`, each session's `prefill()` recorded the OTHER's audio (not silence). Confirm via the fake's `prefill_history` list — at `t=1`, A's prefill was called with the buffer containing B's tick-0 audio (`0.5`) and vice versa. Also assert `metrics.collision_count == 1`, `metrics.max_collision_duration_ticks == 1`.

### Test 3: `test_debate_clean_handover.py`

NOTE: this test validates the Stage 2 approximation (any clean handover counts). Stage 3 refines `self_yields` to require `overlap[s] > 0` per design §3, which will reduce this scenario to 0; rename or replace this test at Stage 3 if it breaks.

Setup: 2 scripted sessions. A speaks (is_listen=False, end_of_turn=False) at t=0, t=1. B is scripted to listen at t=0, t=1 and to speak at t=2. A is scripted to listen at t=2 with `end_of_turn=True` returned at t=1 (i.e., A ends its turn coincidentally just before the challenger speaks). No break is armed (because there's no collision yet — A is alone at t=0, t=1).

Assertion: at t=2, no entry in `break_fired_this_tick`; `audible == ["B"]`; the previous tick had `audible == ["A"]` and `floor == None` (released by end_of_turn at t=1). Confirm `metrics.harness_forced_breaks == 0` and `metrics.self_yields >= 1`.

### Test 4: `test_debate_livelock.py`

Setup: 2 scripted sessions, BOTH speak at every tick (is_listen=False, end_of_turn=False) for ticks 0–4. Run with `max_ticks=3` (collision tick + collision tick + break-fire tick); k_grace=1; rng_seed=0. The scripted fake honors `set_break` by switching to is_listen=True, audio=zeros, end_of_turn=True on the call AFTER set_break was called.

With `k_grace=1`: collision tick 0 → overlap[challenger]=1, no break (1 > 1 is False). Collision tick 1 → overlap[challenger]=2, break armed on incumbent. Tick 2 → break fires on incumbent; resolved.

Assertions:
- Discover the incumbent dynamically: `incumbent = trace.ticks[0].floor` (set by tie_breaker on the first collision). The following assertions reference this `incumbent` rather than hardcoded names.
- `metrics.collision_count == 2` (ticks 0 and 1).
- `metrics.max_collision_duration_ticks <= trace.k_grace + 1` (which is 2).
- Exactly one entry in `metrics.harness_forced_breaks`.
- The incumbent (decided by tie_breaker on tick 0) is the one whose `break_fired_this_tick` is True at tick 2.

### Test 5: `test_debate_deadlock.py`

Setup: 2 scripted sessions, BOTH listen at every tick for ticks 0–4. Run with `max_ticks=5`, `n_deadlock=4`. Moderator nudge audio is a recognizable buffer (e.g., `np.full(CHUNK_SAMPLES, 0.7)`).

Assertions:
- At tick 3 (silence_run reaches 4), `moderator_nudge_fired=True`.
- At tick 4, each session's `prefill_history[-1]` equals the moderator nudge buffer (NOT silence).
- `metrics.moderator_nudges_fired == 1`.
- `metrics.deadlocks_detected == 1`.

## The fake duplex (`tests/_debate_fakes.py`)

```python
@dataclass
class ScriptedTick:
    is_listen: bool
    text: str = ""
    audio_waveform: np.ndarray | None = None     # None => zeros(CHUNK_SAMPLES)
    end_of_turn: bool = False

class ScriptedDuplexSession:
    """In-memory fake honoring set_break semantics from MiniCPM modeling."""
    def __init__(self, name: str, script: list[ScriptedTick]):
        self.name = name
        self._script = list(script)
        self._tick_idx = 0
        self._break_set = False
        self.prefill_history: list[np.ndarray] = []   # appended on each prefill (always the real buffer)
        self.prefill_was_nooped: list[bool] = []      # True when _break_set was True at call time
        self.set_break_calls: list[int] = []          # records _tick_idx at call time, which equals the tick number that just completed generate() — i.e., set_break_calls[-1] == tick_in_which_arm_happened

    def prefill(self, audio_1s: np.ndarray, *, text_list: list[str] | None = None) -> None:
        self.prefill_history.append(np.asarray(audio_1s, np.float32).copy())
        self.prefill_was_nooped.append(self._break_set)

    def generate(self, *, listen_prob_scale: float):
        if self._break_set:
            # Mirror modeling guard: forced listen+silence+eot.
            self._break_set = False  # break fired; the orchestrator clears, but mirror modeling
            self._tick_idx += 1
            return DuplexTickResult(
                is_listen=True, text="", audio_waveform=np.zeros(CHUNK_SAMPLES, np.float32),
                end_of_turn=True, current_time=float(self._tick_idx),
            )
        st = self._script[min(self._tick_idx, len(self._script) - 1)]
        wf = st.audio_waveform if st.audio_waveform is not None else np.zeros(CHUNK_SAMPLES, np.float32)
        self._tick_idx += 1
        return DuplexTickResult(
            is_listen=st.is_listen, text=st.text, audio_waveform=wf,
            end_of_turn=st.end_of_turn, current_time=float(self._tick_idx),
        )

    def set_break(self) -> None:
        self._break_set = True
        self.set_break_calls.append(self._tick_idx)

    def clear_break(self) -> None:
        # Orchestrator calls this AFTER generate fires the break; fake's _break_set is already False.
        pass

    def is_break_set(self) -> bool:
        return self._break_set
```

Note that the fake duplicates the model-side guard (it clears `_break_set` inside `generate` when break was set) AND the orchestrator separately calls `clear_break()`. This intentional double-clear matches the real model's behavior and lets tests assert on `set_break_calls`.

## Verification

```bash
cd /home/yid042/projects/companion-agent-harness/.claude/worktrees/feat+two-minicpm-debate
/raid/yid042/venvs/companion-harness/bin/python -m pytest -q \
  tests/test_debate_acoustic_overlap.py \
  tests/test_debate_clean_handover.py \
  tests/test_debate_livelock.py \
  tests/test_debate_deadlock.py
```

Expected: 4 passed in <2s, no warnings.

## Risks

- The tie-breaker is random; the livelock test must seed `rng_seed=0` and assert based on whichever model the tie-breaker picked, not a hardcoded name.
- `self_yields` derivation in Stage 2 is approximate (it doesn't yet know about `end_of_turn` semantics). The Stage 3 refinement adds the "incumbent's prior tick ended cleanly via end_of_turn" gate. Stage 2 test 3 still passes because its scenario explicitly sets `end_of_turn=True` for A at t=1.

## Open questions (non-blocking)

- Whether the moderator nudge should be a single-tick injection or held across multiple ticks. Default: single-tick (test 5 confirms).
- Whether `_compute_metrics` should be a free function (easier to unit-test in isolation) or a method. Default: method; can be extracted later if needed.
