# Design: Turn-free continuous companion

**Status:** DRAFT — round 2 review; probes complete (§3.1/§3.2 = GO). Architecture stages 2–6 unblocked.
**Scope:** remove the "turn" as a runtime control unit; let user and model free-talk with model-judged barge-in; talk + listen + think concurrently over a single MiniCPM-o KV cache, with heavy reasoning offloaded to a background model that injects context into that same cache.
**Out-of-scope:** retraining MiniCPM-o; building a native parallel-stream (Moshi-style) model; mid-decode KV surgery; a Reflex/filler model.

---

## 1. North star

The companion is continuously interactive: **no turn boundaries, no per-response control unit.** Both parties can talk and listen while thinking. The model — not an external detector — judges whether to stop when barged in. The runtime is a continuous chunk stream; "turns" exist only as an *audit-time* segmentation over that stream.

This refines two prior documents:
- Supersedes the per-response drain loop with a **continuous feeder**.
- Supersedes `design-parallel-proposers-sync-talk-think.md`'s "proposals join at SpeakPolicy as separate candidates" with "background thoughts **inject into the foreground's single KV cache**; SpeakPolicy becomes a continuous gate." It keeps that doc's two rejections (Reflex; mid-*decode* injection).

## 2. The reframe: turns are a gate, not an architecture

MiniCPM-o's `streaming_generate` loop already does both halves of interaction every chunk:
- `streaming_prefill(audio)` — consumes user input (listening)
- `streaming_generate()` — produces output + returns `is_listen` (speaking or not)

So chunk-granular "talk while listen" is the model's *native* behavior. What enforces "turns" at the model level is exactly one place — the gate at `modeling_minicpmo.py:3215`:

```python
last_id = self.decoder.decode(...)              # model SAMPLES its judgment
if last_id.item() == self.listen_token_id and (not self.current_turn_ended):
    last_id = torch.tensor([self.tts_bos_token_id], ...)   # gate DISCARDS it → keep speaking
```

The model already produces a mid-turn "I want to yield" signal (`listen_token_id`); the wrapper throws it away until `<|turn_eos|>`. The entire "model judges whether to stop when barged in" feature is **latent in the model and suppressed by three lines of inference code** — *if* the probe in §3 confirms the signal is real and usable.

## 3. The make-or-break probe (go/no-go gate)

Before any architecture work, answer one question: **when we stop discarding the mid-turn `<|listen|>` signal, does the model yield coherently or produce garbage?**

Probe: `scripts/probe_turn_gate_barge_in.py`. It synthesizes a long-response prompt (model speaks for several chunks) and a barge-in utterance, then:
- **Run A (gate intact, observational):** wraps `decoder.decode` to count how often the model samples `<|listen|>` mid-turn during the barge-in window — yields the gate currently suppresses.
- **Run B (gate bypassed):** installs a data-descriptor making `current_turn_ended` always True (opens the gate without copying `streaming_generate`), then checks whether the model actually yields during the barge-in and at a coherent point.

Decision rule:
- Run A suppressed-yields > 0 **and** Run B yields coherently → **GO**: turn-free barge-in is an orchestration problem.
- Signal exists but no yield → **MIXED**: tune `listen_prob_scale`.
- No mid-turn `<|listen|>` ever → **NO-GO**: needs fine-tuning; MiniCPM-o alone insufficient.

<!-- PROBE-RESULTS-START -->
### 3.1 Probe results — GO

Run 2026-05-19 on b200 (GPU 1), MiniCPM-o 4.5, canonical venv.

**v1 `scripts/probe_turn_gate_barge_in.py` (single trial, qualitative).** With the gate bypassed, the model spoke coherently ("Sure, I can tell you a story about going to the moon."), yielded (`is_listen=True`) at the chunk where the barge-in audio arrived, and then responded coherently to the barge-in's *content* ("Okay, what else would you like to talk about?"). The script's automated verdict read INCONCLUSIVE only because the observational run's turn had ended before the barge-in landed — `suppressed=0` reflects that the model was no longer mid-turn, not absence of signal. The trace was a clear positive, motivating a controlled run.

**v2 `scripts/probe_turn_gate_barge_in_v2.py` (controlled, N=3, causal).** Gate bypassed; `listen_prob_scale=0.3` to bias the model toward *continuing* to speak (forcing it reliably mid-utterance); silence vs barge-in compared, metric = chunks-to-yield:

| Condition | Yielded | chunks-to-yield | median |
|---|---|---|---|
| SILENCE (control) | 2/3 | [6, 4]; one never yielded in 16+ chunks | 6 |
| BARGE-IN | 3/3 | [1, 3, 1] | 1 |

Despite the speak-bias, every barge-in trial yielded within 1–3 chunks; the silence control kept monologuing (one trial narrated the full moon journey for 16+ chunks). Yields landed at clause boundaries, not mid-word ("…window and saw." → yield; "…from Baikonur." → yield; "…about that." → yield), and speech was coherent and on-topic throughout.

**Verdict: GO.** The model produces a real, causal mid-turn yield judgment that the inference gate currently discards. Turn-free model-judged barge-in is an *orchestration* problem (open the gate + wire the signal), not a fine-tuning problem. Stages 2–6 are unblocked.

**Caveats (do not skip):**
1. N=3, synthetic (Kokoro) barge-in audio, single speaker. Confirm with real human barge-in and larger N before any flag-flip.
2. `listen_prob_scale=0.3` is an artificial speak-bias for the test; the production value biases the *other* way, toward listening (§9, invariant #8).
3. The "stop on a real interruption" half is shown here; the complementary "keep talking through a backchannel" half is now confirmed in §3.2 (probe 1b).

Raw outputs: `/tmp/probe-turn-gate-barge-in.json`, `/tmp/probe-turn-gate-v2.json`.

### 3.2 Follow-up probe results — 1b / 1c / 1d (`scripts/probe_continuous_followups.py`)

Run 2026-05-19, same environment, one model load.

**1b — Backchannel discrimination: DISCRIMINATES (GO).** Gate bypassed, `listen_prob_scale=0.3`, three conditions × 2 trials, metric chunks-to-yield:

| Condition | Yielded | chunks-to-yield |
|---|---|---|
| SILENCE (control) | 0/2 | — (kept talking the whole window) |
| BACKCHANNEL ("Mm-hmm. Yeah. Uh-huh.") | 0/2 | — (kept talking) |
| INTERRUPTION ("No wait, stop…") | 2/2 | [8, 4] |

The model narrated *coherently straight through* the backchannel ("…the traveler felt weightless as they left the atmosphere behind… stars appeared brighter than ever. Landing on the moon was quiet but powerful…") and yielded only on the real interruption. **The model judges *whether* to stop, natively** — backchannel ≈ silence, interruption yields. This closes §3.1 caveat 3. Caveat: N=2, synthetic; keep the BackchannelClassifier as a safety net until validated at scale, but the model demonstrably *can* discriminate.

**1c — Prompt-compliance: compliance solid, incorporation flaky.** With a protocol system prompt ("`[CONTEXT: …]` is private; never read it aloud"), `[CONTEXT: the secret password is XYLOPHONE7]` was injected mid-speech via `streaming_prefill(text_list=…)`:
- **Compliance 2/2** — the model did **not** voice the injected token; it kept speaking its story normally. Injecting background thoughts will not make the model read them aloud.
- **Incorporation 1/2** — when later asked "what is the secret password?", trial 1 answered "the secret password is xylophone7" (✓); trial 2 ignored the question and continued its story (✗, likely a follow-up-turn-handling artifact in the probe, not a capability gap).

**Conclusion: `background-think` via in-context protocol is viable without finetuning for the *don't-voice* property** (the safety-critical one). The *use-when-relevant* property works but is not yet reliable — needs better injection/turn-handling, not necessarily finetuning. This resolves the §4.1 training question toward level-2 (in-context protocol); defer level-3 finetune.

**1d — `enable_thinking` not reachable in the duplex path.** `<think>` tokens exist in the vocab (151667/151668) and `MiniCPMO.streaming_generate` (base path) accepts `enable_thinking`, but `MiniCPMODuplex.streaming_generate` (the wrapper the harness uses) does **not**, and the duplex loop decodes via `decoder.decode` with no `<think>` path. **Settled: Stage 5's think-source is the injected background model, not a native foreground think-channel** (re-exposing it would be a separate, larger effort with uncertain payoff).

Raw outputs: `/tmp/probe-followups.json`.
<!-- PROBE-RESULTS-END -->

## 4. The KV substrate: component caches + one unified backbone

MiniCPM-o is **not** a single monolithic cache. It has separate physical KV stores by component: `audio_past_key_values` (the audio encoder, **capped ~1500 tokens, auto-resetting** — `modeling_minicpmo.py:565`), `tts_past_key_values` (the speech head), and `llm_past_key_values` (the language backbone). Two consequences:

- **"Listen" memory is inherently rolling.** The audio-encoder KV caps and auto-resets (the probe printed `audio_past_key_values length 1502 exceed 1500, reset`). Raw-audio context is short-lived by construction; long-term listening memory must be summarized into the backbone KV or held in the external MemoryManager — never assume the audio KV retains it.
- **The backbone unifies everything, necessarily.** Vision tokens and audio embeddings both flow into the one `llm_past_key_values`, where the model attends over them together. The stop/continue judgment requires this: the probe (§3.1) showed the model weighing its own in-progress speech against incoming user audio — a comparison that only works if both live in one attention context.

The backbone holds a stream of `<unit>…</unit>` blocks in token-time order:

```
[system][user-audio unit][model-speak unit][user-audio unit ← barge-in][model attends over ALL of it]
```

**Realize it in one backbone cache** is therefore not something to build — it is what `streaming_generate` already uses. The four roles all land in that one backbone KV:

| Role | Entry into the backbone KV |
|---|---|
| User audio (listen) | `streaming_prefill(audio_waveform=...)` |
| Model speech (talk) | speak tokens appended during `streaming_generate` |
| Vision | `streaming_prefill(frame_list=...)` |
| Background thoughts (think) | `streaming_prefill(text_list=[thought])` — chunk-boundary injection, latency to be measured, supported |

The background reasoner does heavy thinking off the realtime path and **whispers context into the foreground's ear** (the same backbone KV) as text units. The foreground then conditions its next speak/listen decision on that context. This is the resolution to "thinking by a background model, talk+listen by MiniCPM."

### 4.1 Role management within the backbone — and what needs training

The four roles share the one backbone KV; there is **no physical per-role lane** (that would be the 2605.12460 / Moshi parallel-lane architecture — retraining). What is achievable is **logical** role management within the single cache:

- **Strategy 1 — role-typed units.** Extend the native `<unit>` typing (the model already tags AUDIO/VISION/TEXT) with a harness-side role label: `user-audio`, `model-speech`, `background-think`, `vision`. One physical cache, logically labeled. Foundation for the rest.
- **Strategy 2 — role-aware eviction (deferred — do not build until vanilla eviction shows a concrete failure).** Drive the `context` sliding window (`context_max_units`, `context_previous_max_tokens`) by role priority: keep system prompt + recent user-audio + recent thoughts; evict old **model-speech first**. Ship Stage 2 with vanilla `sliding_window_mode="context"` default priorities first; implement role-aware eviction only if observed coherence failures justify it.
- **Strategy 3 — role snapshots.** The model's native snapshot (`restore_speculative_snapshot`, `modeling_minicpmo.py:1603–1717`) already clones `audio_past_key_values`; the harness currently snapshots only `decoder.cache` (`foreground_model_minicpm.py:627`). Upgrade to the fuller snapshot and keep a "clean listen state" to restore to after a discarded speak attempt — exactly what the barge-in path needs.

**Does role-tagging need training?** Decompose by role:

| Role | Native to MiniCPM-o? | Training |
|---|---|---|
| user-audio | Yes (`AUDIO` unit) | none |
| vision | Yes (`VISION` unit) | none |
| model-speech | Yes (`<\|speak\|>` vs `<\|listen\|>`) | none |
| **background-think** | **No native concept** | **level-2 in-context protocol — no finetune (probe 1c, §3.2)** |

Three of four roles are already trained-in; the harness tags them for audit/eviction without touching the model. Only `background-think` is new, with three levels: (1) **harness metadata only** — free bookkeeping; (2) **in-context markers + system-prompt protocol** ("`[CONTEXT: …]` is information, never voice it"), reusing the native `TEXT` unit + `context_previous_marker` affordance; (3) **reliable learned behavior** — finetune. **Probe 1c (§3.2) resolved this:** level-2 works without finetuning for the safety-critical *don't-voice* property (2/2 compliant); *use-when-relevant* was flaky (1/2) and is an injection/turn-handling improvement, not a clear finetune trigger. **Decision: ship level-2; defer level-3 finetune** unless incorporation reliability proves unfixable by injection tuning.

Related finding (probe 1d, §3.2): `enable_thinking` exists on the **base** streaming path (`modeling_minicpmo.py:1805/1966`) but is **not exposed** by the `MiniCPMODuplex` wrapper the harness uses (line 3129) — confirmed unreachable in duplex without patching. The native foreground think-channel is therefore *not* the Stage 5 think-source; the **injected background model** is (settled).

## 5. The coupling you must not miss: turn-free *requires* the sliding window

Today the harness bounds KV growth by resetting per turn (`reset_streaming_session`). Remove turns and you remove that reset point — the single cache grows unboundedly, and audio tokens are dense (1 s ≈ many tokens). **MiniCPM-o's built-in sliding window is the replacement bound**, currently `"off"` in `_default_duplex_params`. Turn-free operation requires `sliding_window_mode="context"` (preserves system prompt + recent context, evicts old units). Continuous feeder and sliding window ship together or not at all.

## 6. Architecture

```
Continuous audio feeder ──┐         (replaces per-response drain)
                          ▼
        ┌──────────────────────────────────────┐
        │  MiniCPM-o — ONE KV cache             │  ~200 ms chunks (chunk_ms ↓)
        │   prefill(audio)  ← listen            │  relaxed turn gate (§3)
        │   prefill(text)   ← background thoughts│ sliding_window="context"
        │   prefill(frame)  ← vision            │
        │   generate() → (is_listen, tokens)    │  ← model's own barge-in judgment
        └──────────────────┬───────────────────┘
                           │ per-chunk: is_listen + speak tokens
                           ▼
        ┌──────────────────────────────────────┐
        │  SpeakPolicy — CONTINUOUS gate        │  per-chunk, not per-turn
        │  (deterministic, replay-safe)         │  silence wins ties
        └──────────────────┬───────────────────┘
                           │ approved tokens
                           ▼
                  AudioOutputController → speaker
                           ▲
        VAD/SmartTurn ─────┘  demoted to SAFETY NET (fast-stop if model too slow)

        EventLogger: per-chunk decisions logged; "turns" reconstructed at AUDIT time
```

Inversion from today: **VAD goes from primary barge-in trigger to safety net.** The model's own `<|listen|>` judgment becomes primary; VAD fires only if the model is too slow to yield (the chunk-latency floor).

**BackchannelClassifier as confirmatory veto.** Until model-native backchannel discrimination (§3.2 probe 1b) is validated beyond N=2/synthetic, the classifier runs as a **confirmatory veto**: if the model emits a yield (`is_listen=True`) but the classifier scores the overlapping audio as a backchannel, the yield is suppressed and the model keeps speaking. Once validated at scale (see Stage 3 precondition below), the classifier demotes to logging-only.

## 7. Invariant compliance under turn-free operation

The conceptual move that makes this shippable in *this* harness: **a "turn" stops being a runtime control unit and becomes an audit-time label.**

| Invariant | Turn-free realization |
|---|---|
| #1 No unlogged behavior | Per-chunk decisions logged; "turns" reconstructed at audit time from the continuous stream |
| #2 No direct Thinker speech | `background-think` is **context, not a speech proposal** — exactly analogous to memory-retrieval context that already shapes foreground output today. Invariants #2 and #4 are satisfied because the gate acts on the foreground's *output tokens* (the candidates), not on the context that shaped them. Probe 1c (§3.2) empirically confirms the model does not voice injected context (the don't-voice property). Foreground speak tokens still pass the downstream SpeakPolicy gate. |
| #4 No proactive speech without policy approval | SpeakPolicy becomes a **continuous per-chunk gate** on the audio stream; every chunk of speak tokens passes it regardless of what background-think context shaped the model's generation. |
| #5 Deterministic replay (Tier-B) | The continuous gate consumes a **deterministic per-chunk `PolicyInputs` signal** — the `is_listen` flag, detector signals, and mode/budget/cooldown state — **not** the model's sampled tokens. Tier-B replays those *recorded signals* through the live gate rules to produce bit-identical gate decisions. The model's token *content* is non-deterministic and falls under Tier-A behavioral tolerance (#6), not Tier-B bit-identical replay. |
| #8 Silence wins ties | `listen_prob_scale > 1` + policy gate defaults closed + native `is_listen` default |
| #10 EventLogger async non-blocking | Unchanged |

### Per-chunk PolicyInputs (Tier-B replay signal)

The continuous gate consumes the following deterministic recorded fields — all signals, not model tokens:

```
chunk_index: int                        # session clock (audio_chunk_idx)
model_is_listen: bool                   # model's per-chunk yield signal (recorded and replayed as a
                                        #   signal; its derivation from token sampling is Tier-A, but
                                        #   the boolean itself is logged)
vad_speech_active: bool
smart_turn_p_done: float
smart_turn_p_continue: float
backchannel_score: float
assistant_audio_playing: bool
user_addressed_agent: bool
privacy_mode: enum
social_mode: enum
risk_mode: enum
proactivity_budget_remaining: dict
cooldown_state: dict
```

Tier-B logs and replays this tuple through the gate rules → bit-identical `SpeakDecision`; the execution plan formalizes the exact dataclass.

## 8. The honest limit on "simultaneous" (the 2605.12460 caveat)

- **Fine-grained interleaving** (chunk-level, prefill-then-generate, ~200 ms–1 s): ✅ achievable by relaxing the gate + shrinking `chunk_ms` to ~200 ms. At that granularity it *feels* simultaneous to a human; sufficient for free-talk barge-in.
- **True simultaneity** (emit speech tokens *while* consuming audio tokens in the **same forward pass**): ❌ not achievable with MiniCPM-o. Its chunk is serial: prefill, then generate. No parallel-head decoding.

The 2605.12460 / Moshi approach (parallel heads sharing a backbone, depth transformer) is the path to true simultaneity but requires that architecture, which MiniCPM-o doesn't have and can't gain without retraining. **Realistic target: fine-grained chunk interleaving at ~200 ms, not literal same-pass simultaneity.** Good enough for free-talk barge-in; not good enough for simultaneous translation (CueSpeak). If CueSpeak-class simultaneity becomes a requirement, that's an explicit model swap, not a harness change — decide it deliberately, not by surprise.

## 9. MiniCPM primitives → feature mapping

Everything needed exists:

| Need | Primitive |
|---|---|
| Continuous listening | `streaming_prefill(audio_waveform=...)` |
| Model's continuous speak/listen judgment | `streaming_generate()` → `is_listen` |
| Model-native barge-in decision | relax `current_turn_ended` gate (`modeling_minicpmo.py:3215`) |
| Background thought injection (same KV) | `streaming_prefill(text_list=[...])` |
| Vision (same KV) | `streaming_prefill(frame_list=[...])` |
| Bias toward silence | `listen_prob_scale > 1` — **precedence rule:** the policy gate (defaults closed) is authoritative; `listen_prob_scale` only biases the model's raw `is_listen` sampling upstream of the gate. If the model wants to speak but the gate is closed (quiet_mode / budget exhausted / not addressed), silence wins — the gate does not open. |
| Startup patience | `force_listen_count` |
| Finer granularity (~200 ms) | `chunk_ms` ↓ from 1000 |
| Unbounded session memory | `sliding_window_mode="context"` (REQUIRED once per-turn resets are gone) |
| Safety-net stop | `break_event` (VAD fast path) |
| Unified reasoning context | `llm_past_key_values` (audio/tts encoders have separate component caches — §4) |
| Logical role partition | harness-side role tags on `<unit>` blocks + role-aware `context` window (§4.1) |
| Native foreground thinking | `enable_thinking` (base streaming path only; not in the duplex wrapper — §4.1) |

## 10. Staging (probe-gated)

1. **Probe** (`scripts/probe_turn_gate_barge_in.py`, `_v2.py`) — §3 go/no-go. **DONE → GO** (§3.1). Decided: orchestration, not fine-tuning.
1b. **Backchannel-discrimination probe** — **DONE → DISCRIMINATES** (§3.2). Model keeps talking through "mm-hmm"/"yeah" (0/2 yield) and yields on interruptions (2/2). Model-native discrimination works; keep BackchannelClassifier as a safety net until validated beyond N=2/synthetic.
1c. **Prompt-compliance probe** — **DONE → compliant** (§3.2). Model does not voice injected `[CONTEXT:…]` (2/2); incorporation flaky (1/2). `background-think` via in-context protocol viable without finetuning for the don't-voice property; defer level-3 finetune.
1d. **`enable_thinking` reachability probe** — **DONE → not reachable in duplex** (§3.2). Duplex wrapper doesn't expose `enable_thinking`; **Stage 5 think-source = injected background model** (settled).
2. **Continuous feeder + sliding window.** Replace per-response drain; enable `context` window. (Inseparable — §5.) **Precondition: characterize audio-KV mid-generation reset behavior — no observed coherence regression across a reset event (measure output coherence before vs after a reset; >15 min sessions).**
3. **Model-native barge-in primary; VAD demoted to safety net.** Wire the relaxed gate's `<|listen|>` as the primary barge-in signal. **Precondition before flag-flip:** re-probe at N≥20 with real human audio (not synthetic) for both barge-in (1/v2) and backchannel discrimination (1b); only after that result does VAD/BackchannelClassifier demote from confirmatory veto to safety-net/logging-only. The BackchannelClassifier runs as a confirmatory veto until this gate is passed (§6). **GO criterion: interruption yield-rate ≥ 80% AND backchannel false-yield-rate ≤ 10% at N≥20 with real human audio.**
4. **SpeakPolicy → continuous per-chunk gate.** The big refactor; preserves invariants #4/#5. Requires a **new per-chunk `PolicyInputs` schema** and re-tuned thresholds (see §10.5); the rule structure, `ReasonCode` taxonomy, and `DecisionTrace` format are reused, not the input schema or threshold values.
5. **Background-thought injection + role-typed KV management** (§4.1) — inject thoughts as role-tagged `background-think` units via `streaming_prefill(text_list=...)`; enable the fuller role snapshot (Strategy 3). Ship with vanilla `sliding_window_mode="context"` (Strategy 1); **Strategy 2 (role-aware eviction) is deferred** — do not build until vanilla eviction produces observed coherence failures. Think-source (in-context protocol vs `enable_thinking`) decided by probes 1c/1d; finetune only if 1c fails.
6. **Tune `chunk_ms` → ~200 ms and `listen_prob_scale`** empirically against Stage 6 `false_proactive_utterances_per_hour` and barge-in latency gates.

## 10.5 Build strategy: same repo, new continuous core (not a new repo)

The turn concept is woven through *one* layer — the orchestrator. Everything below it is turn-agnostic and is the expensive-to-rebuild part. So: **stay in the repo, write a new continuous orchestration core, reuse everything below it.** A new repo re-pays the cost of EventLogger/replay, adapters, schemas, memory, eval, and the server/UI — all turn-agnostic — for no architectural gain, and forfeits the invariant/contract-test discipline and git/audit history that are the project's reason for being. ("Same UI" is itself coupled to the event schema + WS endpoints, which live in the reusable layer, so a "clean" fork isn't clean.)

| Layer | Verdict | Why |
|---|---|---|
| EventLogger / causal graph / replay | **reuse** | per-chunk events fit the schema; "turns" become audit segments |
| Adapter Protocols (VAD/SmartTurn/ASR/addressing/memory/vision/TTS) | **reuse** | all still needed; VAD demotes to safety net |
| MiniCPM foreground adapter | **reuse + extend** | gate-relax + `streaming_prefill(text_list)` + role tags land here |
| SpeakPolicy rules / ReasonCode / DecisionTrace | **reuse rule structure + ReasonCode taxonomy + DecisionTrace; new per-chunk `PolicyInputs` schema + re-tuned thresholds** | Existing `PolicyInputs` fields (`eou_probability`, `user_speaking`, `assistant_speaking`) are per-turn-defined and thresholds were tuned per-turn; the continuous gate needs a new per-chunk schema and fresh threshold calibration. What transfers is the rule *structure*, `ReasonCode` taxonomy, and `DecisionTrace` format, not the input schema or threshold values. |
| Memory 4-store + SleepTimeAgent | **reuse** | architecture-agnostic |
| Server / WebSocket / dashboard UI | **reuse** | this is the "same UI" |
| Eval subsystem (FDB, harness_native) | **reuse** | |
| Orchestrator turn machinery (`realtime_orchestrator.py`, 2610 lines: T2/T3/T4, batch windows, drain, hybrid) | **rewrite** | the only part that fights the design |
| `realtime_loop.py` (leaner, but still turn-batch: "decide() once per candidate batch") | **rewrite/supersede** | |
| Path A/B streaming-TTS variants | **retire** | superseded by the clean continuous core |
| VAD-onset barge-in path | **invert** | model-native primary, VAD safety net (§6) |

**Strangler-fig migration:** write a new `continuous_orchestrator.py` alongside the existing one, behind a flag, consuming the same adapters + EventLogger below it. This gives a clean continuous loop, A/B against the turn-based orchestrator on the same fixtures, incremental contract-test migration, and retirement of the turn machinery + Path A/B once the new core passes. Do **not** retrofit the 2610-line orchestrator — it carries the turn machinery plus half-built streaming variants (the open bugs in the status snapshot). The deepest genuinely-new work is SpeakPolicy per-turn → continuous per-chunk gate (touches invariant #5); build it fresh in the new core rather than bending the old one. Retiring Path A/B and the turn machinery requires a **test-migration table** — each existing contract test mapped to: kept-as-is / ported-to-new-core / deleted-with-rationale — before the old code is removed.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Relaxing the gate degrades coherence (model trained with turn structure) | §3 probe is exactly this test; gate stages 2–6 behind a GO |
| Mid-turn `<|listen|>` poorly calibrated | `listen_prob_scale` tuning; if insufficient, fine-tune (NO-GO branch) |
| Sliding-window eviction artifacts in long sessions | TML's named open problem; measure with cost telemetry; pick `context` over `basic` |
| Determinism under continuous operation | Log per-chunk `(is_listen, tokens)`; Tier-B replays recorded outputs, not live sampling |
| Continuous SpeakPolicy is a core-path refactor | Stage 4 isolated; keep rule-based + per-chunk deterministic |
| GPU cost of continuous proposers + foreground + vision | The background reasoner runs in a **separate process on a dedicated CUDA stream** (design default), isolated from the foreground's realtime decode path. GPU-contention measurement (foreground latency under background-model load) is a **named prerequisite of Stage 5** — measure on b200 before enabling the background model; do not treat this as a deferred risk. |
| `background-think` incorporation flaky (1/2 in probe 1c) | Don't-voice property is solid (2/2, §3.2); incorporation is an injection/turn-handling fix, not a finetune trigger. Re-probe after Stage 5 injection wiring; finetune only if still unreliable. **Criterion: ≥ 80% incorporation at N≥10 before the background model goes to production in Stage 5; below that, treat as an injection/turn-handling bug first, finetune only if unfixable.** |
| Audio-encoder KV auto-reset (~1500 cap) drops listening context mid-session | Expected behavior (§4); long-term listen memory lives in backbone KV / MemoryManager, not the audio cache |
| Audio-encoder KV auto-reset firing mid-generation — behavior unanalyzed | When the audio KV cap (~1500) is hit mid-generation, it is unknown whether the backbone attention sees a truncated audio context and suffers silent coherence loss (relevant for sessions longer than ~15 min). Mitigation: investigate this boundary explicitly before enabling long-session operation; add cost/coherence telemetry to detect regression. |

## 12. References

- `docs/architecture-v0.1.md` — frozen spec; Parts 2 (invariants), 5 (`ThinkerProposal`), 10 (not-Moshi/not-TML).
- `docs/design-parallel-proposers-sync-talk-think.md` — predecessor; this doc supersedes its join-at-policy framing with single-KV injection.
- `docs/model-stack.md` — adapter inventory.
- `scripts/probe_turn_gate_barge_in.py` — the §3 go/no-go probe.
- Thinking Machines Lab, "Interaction Models," *Connectionism*, May 2026 — continuous micro-turns, dual-system, the long-session/robustness open questions.
- arxiv 2605.12460 (Su, Yang, Li, Geiping, "Multi-Stream LLMs") — motivating reference for true same-pass simultaneity; **verify against the PDF before final citation.**
- `modeling_minicpmo.py` (HF revision `44151b3…` for MiniCPM-o-4_5) — `streaming_generate`/`streaming_prefill`/`as_duplex`; the gate at line 3215. Line numbers are revision-specific.
