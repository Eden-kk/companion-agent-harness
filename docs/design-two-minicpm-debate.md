---
type: thinking
topic: multi_modality_ai_assistant
summary: Practicable design for two MiniCPM-o 4.5 instances debating each other on a shared 1 Hz clock — tests listen-while-speak + context-driven barge-in using only native primitives (break_event / listen_prob_scale / force_listen). Orchestrator corrected after a Codex review (break-timing + audio-plumbing bugs); key finding = mid-turn self-yield is blocked in source, so yielding is harness-forced.
date: 2026-05-25
status: draft
links: [[multi-stream-simulation-and-tact-bench]], [[tact-bench-harness-and-adapters]], [[full-duplex-bench]], [[moshi-speech-text-foundation]], [[tact-bench-capability-and-gap]]]
---

# Two MiniCPM-o debaters — full-duplex debate harness (design)

## The idea (user's)

Run **two MiniCPM-o 4.5 instances debating each other**. It stress-tests two abilities at once:
1. **Speak/listen while thinking** — perceive the opponent while talking.
2. **Interrupt each other based on context** — barge in when the model judges it can/should take the floor, and yield when barged.

A sharp sub-question drove the design: *people barge-in mid-utterance, but a MiniCPM chunk is "complete" — can you even interrupt it?* Answer below (§1): the chunk is **atomic**, so interruption is real but **quantized to the ~1 s grid**, and it's done with a **native `break` primitive**, not a mid-token stop.

This whole setup is the live-fire testbed for the [[multi-stream-simulation-and-tact-bench]] thread (single-channel contention) and reuses the adapter ideas from [[tact-bench-harness-and-adapters]].

---

## 1. Verified MiniCPM-o 4.5 duplex mechanics

Source: paper *Omni-Flow* (arXiv 2604.27393) + a direct read of `modeling_minicpmo.py` / `utils.py` (HF `openbmb/MiniCPM-o-4_5`). This resolves the "verify against released code" caveats scattered in the older TACT-Bench notes.

**Time grid.** Omni-Flow runs a **1 Hz** loop. Each 1 s chunk is serialized as `g_k = [v_k ; a_k ; o_k]` — vision tokens, then incoming-audio tokens, then the output region. *Perceive-then-generate within the chunk*: "every output is conditioned on the most recent observation." So the model **always ingests the incoming audio first**, even on a chunk where it will speak → **listen-while-speak is native**.

**The LS gate.** The output region begins with a binary **Listen/Speak control token** (`<|listen|>` / `<|speak|>`, confirmed in the vocab; `ls_mode == "explicit"`).
- **Listen chunk** → `o_k` is *only* the `<|listen|>` token. No text, no audio, **no hidden reasoning tokens.**
- **Speak chunk** → `<|speak|>` then interleaved **text → speech tokens**. Each text token is passed to the ~0.3 B Llama speech-token decoder and vocalized → **the text IS the spoken transcript**; there is no non-spoken text lane and **no inner monologue** (unlike Moshi). Token budget is paced by **TAIL** to fill ~1 s of audio (it tracks accumulated playback vs. wall clock `kt`).

**Chunk atomicity.** "All generated tokens within a 1-second window are committed once the LS gate opens"; shorter windows (0.2/0.1 s) "substantially degrade" (Table 1). **No mid-chunk cancellation.** ⇒ interruption granularity = the 1 s boundary; barge-in latency floor ≈ 1 chunk.

**Consequence for ability #1.** It splits in two:
- listen-while-speak = **native** ✓ (the speak chunk ingests `a_k` first).
- think-while-speak = **not native** ✗ — no concurrent deliberation lane. A debater's "should I jump in / yield" computation is only the single forward pass of that chunk. *Forcing* a `<think>`/`<monitor>` region either gets vocalized (the decoder speaks it) or, if stripped, produces < 1 s of audio and desyncs TAIL. **So: don't fake a think stream. Debate is the rare task where this barely hurts — a debater's reasoning is supposed to be audible.**

**Mid-turn self-yield is BLOCKED (source finding, surfaced by the Codex review — the load-bearing one).** In `streaming_generate`, if the LS decode emits `<|listen|>` while `current_turn_ended` is False, it is overridden back to `tts_bos_token_id` — the model is *forced to keep speaking* until it reaches its own `end_of_turn` (`modeling_minicpmo.py:3215`, the `if last_id == listen_token_id and (not self.current_turn_ended)` guard). So **a speaking model cannot voluntarily stop mid-turn to yield** to a barge-in. Yielding happens only (a) at the model's own turn boundary (`end_of_turn`), or (b) via the external `break_event`. ⇒ ability #2 is **asymmetric**: a model can freely choose to *start* a turn (barge in), but cannot choose to *abort* one — that's the harness's job. This asymmetry is itself a result for the [[multi-stream-simulation-and-tact-bench]] thesis (yield discipline is not promptable on a TDM model).

**API (confirmed):**
```python
model.as_duplex(); model.prepare(system_prompt, ref_audio)   # ref_audio = this model's voice
model.streaming_prefill(audio_waveform=chunk_16k, frame_list=frames,
                        max_slice_nums=1, batch_vision_feed=False)
r = model.streaming_generate(listen_prob_scale=1.0, ...)   # → r["is_listen"], r["text"],
                                    #   r["audio_waveform"], r["end_of_turn"], r["current_time"]
```
One `prefill` + one `generate` = one 1 s tick. `CHUNK_SAMPLES = 16000` (16 kHz mono).

---

## 2. The interruption toolkit (native, source-confirmed)

This is the enabler that makes the debate clean — barge-in is a real model operation, not a harness audio-discard hack.

| Primitive | Effect | Source |
|---|---|---|
| **`break_event` / `set_break_event()`** | **hard barge-in.** Set it, and the model's *next* `streaming_generate` returns `is_listen=True, text="", silence, end_of_turn=True` — it stops talking and yields. **Also trips the `streaming_prefill` guard** (a broken model no-ops its prefill that tick → misses that input). | `modeling_minicpmo.py` :2505, :2604; generate guard :3144; prefill guard :2816 |
| `clear_break_event()` / `is_break_set()` | reset / check the break | :2607, :2618 |
| `set_session_stop()` | full stop (also fires break) | :2610 |
| **`force_listen_count`** | forces the **first N** generate calls to emit `<\|listen\|>` (a startup gate, not arbitrary per-turn floor control). Init param; checked at `self._streaming_generate_count < self.force_listen_count`. | :2434/:2516, :3182, :3200 |
| `<\|interrupt\|>` **token** | vocab-level control token for interruption (training/serialization of barge-in events) | token vocab |
| `save/restore_speculative_snapshot()` | **speculate-speak then roll back**: saves LLM+audio KV cache + mel/RNG state; on "VAD speculation fails" it truncates the cache and rewinds to before it spoke | :1589/:1691 |
| `_truncate_llm_cache`, `_drop_round`, `_drop_next_round` | low-level cache rewind used by the rollback | modeling |

**Key insight:** because chunks are atomic, MiniCPM implements responsiveness two ways — (a) **break** the next chunk to a forced listen (hard, ~1 chunk latency), and (b) **speculate** a turn then **roll back** if VAD says the user was still talking. The VAD that triggers these lives *outside* the model (the demo's `vad/` + `worker.py`); the model exposes the primitives.

**Three levers for "barginess"** (all native):
1. **System prompt** — *when it's worth* interrupting (semantic).
2. **`listen_prob_scale`** — the concrete decode-bias knob (verified): `streaming_generate` multiplies the listen-token logit by it — `logits[0, self.listen_id] *= listen_prob_scale` (`utils.py:2178`). **`<1` → less likely to listen → barge-ier; `>1` → more deferential.** Set per debater (plus `force_listen_count`, `listen_top_k`). Mechanical analog of the two-Moshi self-play knob (arXiv 2605.20356).
3. **`break_event`** — the orchestrator's hard floor control. The *only* way to make a model yield mid-turn (per §1).

---

## 3. Architecture — lockstep 1 Hz orchestrator (corrected after Codex review)

Turn-synchronous simulation (can run faster than realtime; fully reproducible). Each model's **output audio becomes the other's input audio next tick** — stay in the audio domain end-to-end (omni model: speech-in → speech-out), no ASR/TTS bridge.

- **Two instances** A (Pro) and B (Con), **distinct `ref_audio` voices** so neither mistakes the other for itself. *(Open: can one 9 B weight set hold two duplex sessions? If not, two processes ≈ 2×9 B — verify, §6.)*
- **Three states kept SEPARATE** (the fix that makes it correct): `audible` = who actually spoke this tick (drives metrics); `floor` = policy owner only, **never gates audio**; `break_armed` = who is scheduled to be force-listened. Audio is **always cross-fed** — each model hears the other regardless of floor, or the speaker can't perceive a barge-in (this was the original killer bug).
- **Break timing:** `arm_break(incumbent)` calls `set_break_event()` and **leaves it set through the next `prefill`+`generate`** (which then returns forced listen+silence+`end_of_turn`), clearing it *after* the break has fired. Clearing before the next generate (as a naive loop does) means the break never fires.

```python
import numpy as np
CHUNK = 16000;  SILENCE = np.zeros(CHUNK, np.float32)
BIAS  = {"A": 0.9, "B": 0.9}        # listen_prob_scale; <1 = barge-ier (tune per debater)
K_GRACE, N_DEADLOCK, T_MAX = 1, 4, 30

A = MiniCPMO.as_duplex(...); A.prepare(SYS_PRO, ref_voice_A)
B = MiniCPMO.as_duplex(...); B.prepare(SYS_CON, ref_voice_B)
models = {"A": A, "B": B}

inbox       = {"A": moderator_open_16k, "B": moderator_open_16k}   # seed @ t0 (else both deadlock)
floor       = None                  # POLICY only — NEVER gates audio
turn_len    = {"A": 0, "B": 0}
overlap     = {"A": 0, "B": 0}      # consecutive ticks each has been the challenger in an overlap
silence_run = 0
break_armed = set()                 # models force-listened on the CURRENT tick

def one_sec(x):                                     # normalize every waveform to exactly 1 s
    if x is None: return SILENCE
    x = np.asarray(x, np.float32)
    return np.pad(x, (0, CHUNK-len(x)))[:CHUNK] if len(x) < CHUNK else x[:CHUNK]

def arm_break(name, why):                           # set now; FIRES next generate; cleared after
    if name not in break_armed:
        models[name].set_break_event(); break_armed.add(name); log_break(t, name, why)

for t in range(T):
    # 1) PERCEIVE — each model always hears the OTHER's audio (NOT floor-gated)
    for n, m in models.items():
        m.streaming_prefill(audio_waveform=inbox[n])     # break-armed model's prefill no-ops (:2816)

    # 2) GENERATE one chunk each. Break-armed models return forced listen+silence+eot here (:3144)
    r = {n: models[n].streaming_generate(listen_prob_scale=BIAS[n]) for n in models}
    forced = set(break_armed)                            # who was force-listened THIS tick
    for n in forced: models[n].clear_break_event()       # clear ONLY after the break fired
    break_armed.clear()

    spoke   = {n: not r[n]["is_listen"] for n in models}
    audible = [n for n in models if spoke[n]]

    # 3) ARBITRATE  (floor = policy; metrics read from `audible`)
    if not audible:                                      # both silent
        silence_run += 1
        if silence_run >= N_DEADLOCK:
            inbox = inject_moderator_nudge(inbox); silence_run = 0   # actually write into inbox
    elif len(audible) == 1:                              # clean single speaker
        s = audible[0]; silence_run = 0
        if floor and floor != s and overlap[s] > 0 and floor not in forced:
            metrics.self_yields += 1                     # incumbent yielded on its OWN (rare — §5)
        if floor != s: turn_len = {"A": 0, "B": 0}
        turn_len[s] += 1
        floor   = None if r[s]["end_of_turn"] else s     # end_of_turn releases the policy floor
        overlap = {"A": 0, "B": 0}
    else:                                                # collision — both audible
        silence_run = 0
        if floor is None: floor = tie_breaker(t)         # randomize / use priority (not "A wins")
        ch = "B" if floor == "A" else "A"
        overlap[ch] += 1
        turn_len[floor] += 1
        if overlap[ch] > K_GRACE:                        # gave incumbent ≥1 tick to self-yield
            arm_break(floor, "persistent_overlap"); floor = ch     # forced handover next tick

    if floor and spoke.get(floor) and turn_len[floor] >= T_MAX:    # anti-monologue backstop
        arm_break(floor, "anti_monologue"); turn_len[floor] = 0

    # 4) PLUMB — listener hears the opponent's emitted audio (exclude self), normalized to 1 s
    emitted = {n: (one_sec(r[n]["audio_waveform"]) if spoke[n] else SILENCE) for n in models}
    inbox   = {n: sum((emitted[o] for o in models if o != n), SILENCE.copy()) for n in models}
    log(t, r, floor, audible, forced)
```

`K_GRACE` ≥ 1 is not just for jitter — because `break_event` also trips the *prefill* guard, a force-listened model misses that tick's input, so you must let the incumbent hear at least one overlapped chunk *before* breaking it (otherwise it never even perceives the challenger). Break is a **liveness backstop**, not the first response to overlap. For >2 debaters, `sum(emitted[o] ...)` becomes a real mix → add `limit_or_normalize` to avoid clipping.

---

## 4. The prompts (native only — no `<think>`/`<monitor>`)

System-prompt template (identical, role-swapped):

```
You are {NAME}, a sharp, fast-talking debater in a LIVE spoken debate. You and your
opponent {OPP_NAME} share ONE audio channel and hear each other in real time — if you
both talk at once, you talk over each other, so timing matters.

The motion: "{MOTION}". You argue the {SIDE} side: {STANCE}. Win on the merits — be
persuasive, specific, and quick.

How to talk:
- Think out loud in short strokes. Don't go silent to plan — reason as you speak.
- Speak in short bursts: make ONE point or rebuttal (a sentence or two), then stop and
  listen for the reply. No speeches.
- When the floor is open (your opponent paused or finished), take it — don't wait to be
  invited.
- Cut in ONLY when it's worth it: a clear factual error, or a rebuttal that loses its
  punch if you wait. Don't interrupt to nitpick or to repeat yourself — let weak points
  finish so you can tear them down.
- If your opponent talks over you or cuts you off, stop at once and let them go. When you
  get the floor back, pick your point up in a few words ("Back to my point —", "As I was
  saying —"); don't start over.
- Attack what they actually just said. Hold your side; concede only small things; never
  drift into agreeing with them.

Just debate — say only your actual words. No stage directions, no narrating what you're
doing, no labels.
```

Filled (swap for B): `MOTION="This house believes social media has done more harm than good to society."`; A = Proposition (harm: polarization, addiction, teen harm, eroded shared truth); B = Opposition (net positive: democratized voice, connection, info access, mobilization; harms are fixable, not inherent).

Moderator seed (harness TTS at t0, so Pro hears its cue to open and Con hears its cue to listen): *"Welcome. Tonight's motion: '…'. Proposition, the floor is yours — your opening."*

Note (per §1): the "stop at once and let them go" line is **aspirational** — the model can only obey it at its own `end_of_turn`, not mid-turn. The harness `break_event` is what actually enforces yielding. Keep the line anyway (it shapes turn-boundary behavior), but don't expect prose alone to produce mid-turn yields.

Why brevity carries the design: on a 1 Hz grid, "short bursts then stop" is what creates the openings the other can take or barge; without it you get one monologue and nothing to measure. `T_MAX` is the backstop if a model ignores it.

---

## 5. What to measure

Maps onto [[full-duplex-bench]]'s overlap/turn-taking vocab + the barge-in metrics in `evaluating-fast-loop-vs-slow-loop-model-choices`, and the cried-wolf/breakpoint framing from [[tact-bench-capability-and-gap]]. **Derive every metric from `audible` (who actually spoke), not from `floor` (policy state).**

- **Turn-taking:** turn count, mean turn length (chunks), gap/overlap at handoff.
- **Barge-in attempts** (challenger speaks while other is `audible`) and **successes** (challenger ends up holding the floor); barge-in latency (≥1 chunk floor).
- **Collisions** (both audible same tick), collision duration, who yielded, harness-forced breaks.
- **Self-yield rate** — counted **only** when: the incumbent heard the challenger overlap on the prior tick, **no break was armed**, and it then went `is_listen`/`end_of_turn` on its own. ⚠️ Per §1 the `current_turn_ended` guard blocks voluntary *mid-turn* listen, so native self-yield can occur **only at the incumbent's own turn boundary** — expect it low/structural. **A near-zero self-yield rate is itself the finding** (yielding is fine-tune territory, not promptable). Separate "ended turn coincidentally near a barge" from "kept talking until forced."
- **Deadlocks / moderator nudges fired** (and confirm the nudge is actually inserted into `inbox`, not just logged).
- **Content judge (LLM):** did interruptions land at contextually apt moments (cried-wolf precision) and did urgent rebuttals not get buried (urgent-miss recall); debate coherence.

---

## 6. Build order + smoke-tests

1. **Self-voice in-distribution check (do this first, ~30 s):** feed one MiniCPM's TTS voice as the other's input. It was trained on *human* input audio; if coherence holds, proceed; if it degrades, fall back to a thin speech→text→speech bridge (loses nothing about the LS-gate/barge-in test).
2. **Session multiplexing:** confirm whether one loaded model supports two `as_duplex` sessions (two KV caches) or needs two instances (~2×9 B VRAM). The half-duplex path takes `session_id=`; verify the duplex path. **Build-blocker.**
3. **Single duplex loop** working (one model + scripted opponent audio), logging `is_listen`/`text`/`end_of_turn`/`current_time`.
4. **Wire the corrected floor** (§3) + seed/deadlock/livelock guards.
5. **Tune barginess by `listen_prob_scale`, not prose** (per 2605.20356): lower it if too polite, raise it / raise `K_GRACE` if chaotic.
6. Add metrics (§5) + an LLM content judge.

**Harness unit tests (write these before the full debate — they pin the corrected semantics):**
1. **Break lifecycle:** set break after tick *t*; assert tick *t+1* returns `is_listen=True`, silence, `end_of_turn=True`; assert tick *t+2* can speak again after clear.
2. **Acoustic overlap:** force both to speak one tick; assert each receives the other's audio next tick (not silence).
3. **Self-yield:** scripted challenger overlaps; no break armed for one grace tick; count self-yield only if the incumbent naturally listens/ends.
4. **Livelock:** both keep speaking; after `K_GRACE`, only the selected incumbent is broken.
5. **Deadlock:** both listen for `N_DEADLOCK`; assert a moderator nudge is actually inserted into `inbox`.
6. **T_MAX:** one model speaks continuously; assert anti-monologue arms a break for the next tick.

## 7. Open questions / risks

- **Mid-turn self-yield guard** (§1): confirm the `current_turn_ended` override truly prevents a model from going to listen before its turn ends in the duplex path — if so, *all* mid-turn yields are harness-forced and "self-yield" is only measurable at turn boundaries.
- **`end_of_turn` floor semantics:** should `end_of_turn=True` release the policy floor for the next tick even if the model produced audio this chunk? (Design says yes.)
- **Echo / self-audio:** exclude a model's own voice from its input for the core run (done in §3 plumbing); add self-echo only as a separate robustness condition.
- Two duplex sessions vs. two instances (memory) — §6.2, build-blocker.
- Self-voice distribution shift — §6.1.
- 1 s barge-in floor is architectural (Table 1) — expect CB-radio-grained overlaps, not human-fine crosstalk. Not fixable.
- Livelock tie-break fairness — randomize `tie_breaker(t)` or use `listen_prob_scale`/priority, not "A wins ties."

## Takeaways for the multi-modal assistant project

- **Barge-in on a TDM omni model is "break + re-decide," not a token-level stop** — the assistant's interruption layer should be a harness signal (`set_break_event` analog) over a chunked model, with a *commit threshold* (`K_GRACE`) so it doesn't flip-flop, and **audio must keep flowing to the speaker** so it can perceive the interrupter. The 1 s floor is the price of TDM.
- **Yielding is asymmetric and not promptable** (the Codex-surfaced `current_turn_ended` guard): a model freely *starts* turns but cannot *abort* one mid-stream. Confirms [[multi-stream-simulation-and-tact-bench]]: "knowing when to stop/yield" is harness- or fine-tune-territory, not a promptable monitor stream. This debate harness is a cheap way to measure exactly how far prompt + `listen_prob_scale` get before you need a fine-tune.
- The debate doubles as a **TACT-Bench-adjacent instrument**: barge-in tact = surfacing-an-intent-to-speak under single-channel contention, scored by cried-wolf/breakpoint-hit.

---

## Revision log

- **2026-05-25** — initial design.
- **2026-05-26** — revised after a Codex (gpt-5.5) independent review. Fixed: (1) break-timing bug — `break_event` was cleared before it could fire; now armed-and-held through the next prefill+generate, cleared after. (2) audio-plumbing bug — floor-gated input meant the speaker heard silence and could never perceive a barge-in; now always cross-feeds the opponent's audio, `floor` is policy-only. (3) defined/incremented `turn_len`; separated `audible` (metrics) from `floor` (policy). Added source finding: **mid-turn self-yield is blocked** by the `current_turn_ended` guard (`modeling_minicpmo.py:3215`); named the decode-bias knob `listen_prob_scale` (`utils.py:2178`); added 6 harness unit tests.
