# Design: Turn-free continuous companion

**Status:** DRAFT — for review (round 0). Gated on a probe result (§3); do not start architecture stages until the probe returns GO.
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
3. **Only the "stop on a real interruption" half is shown.** The complementary half the north star requires — the model *keeps talking through a backchannel* ("mm-hmm") and yields only on a genuine interruption — is **not yet tested**. That is the immediate follow-up probe (feed a backchannel as the condition audio; expect NO yield). Until it passes, keep VAD + BackchannelClassifier as the discrimination layer (§6 safety net) rather than trusting the model's raw yield judgment unconditionally.

Raw outputs: `/tmp/probe-turn-gate-barge-in.json`, `/tmp/probe-turn-gate-v2.json`.
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
| Background thoughts (think) | `streaming_prefill(text_list=[thought])` — chunk-boundary injection, cheap (~10–30 ms), supported |

The background reasoner does heavy thinking off the realtime path and **whispers context into the foreground's ear** (the same backbone KV) as text units. The foreground then conditions its next speak/listen decision on that context. This is the resolution to "thinking by a background model, talk+listen by MiniCPM."

### 4.1 Role management within the backbone — and what needs training

The four roles share the one backbone KV; there is **no physical per-role lane** (that would be the 2605.12460 / Moshi parallel-lane architecture — retraining). What is achievable is **logical** role management within the single cache:

- **Strategy 1 — role-typed units.** Extend the native `<unit>` typing (the model already tags AUDIO/VISION/TEXT) with a harness-side role label: `user-audio`, `model-speech`, `background-think`, `vision`. One physical cache, logically labeled. Foundation for the rest.
- **Strategy 2 — role-aware eviction.** Drive the `context` sliding window (`context_max_units`, `context_previous_max_tokens`) by role priority: keep system prompt + recent user-audio + recent thoughts; evict old **model-speech first** (the model needs its own past utterances less than the user's). This is the §5 unbounded-growth fix made role-aware.
- **Strategy 3 — role snapshots.** The model's native snapshot (`restore_speculative_snapshot`, `modeling_minicpmo.py:1603–1717`) already clones `audio_past_key_values`; the harness currently snapshots only `decoder.cache` (`foreground_model_minicpm.py:627`). Upgrade to the fuller snapshot and keep a "clean listen state" to restore to after a discarded speak attempt — exactly what the barge-in path needs.

**Does role-tagging need training?** Decompose by role:

| Role | Native to MiniCPM-o? | Training |
|---|---|---|
| user-audio | Yes (`AUDIO` unit) | none |
| vision | Yes (`VISION` unit) | none |
| model-speech | Yes (`<\|speak\|>` vs `<\|listen\|>`) | none |
| **background-think** | **No native concept** | **the open question** |

Three of four roles are already trained-in; the harness tags them for audit/eviction without touching the model. Only `background-think` is new, and it has three levels: (1) **harness metadata only** — free bookkeeping for audit/eviction; (2) **in-context markers + system-prompt protocol** ("`[CONTEXT: …]` is information, never voice it"), reusing the native `TEXT` unit + `context_previous_marker` affordance — free to try, reliability unproven under streaming decode; (3) **reliable learned behavior** — finetune on streaming data where think-context is injected and the model learns to incorporate-not-voice it. **Discipline: probe level 2 first; finetune (level 3) only if prompt-only proves unreliable.** Do not finetune speculatively — background-think may piggyback on the native `TEXT`/context-injection path well enough.

Related finding: `enable_thinking` is a real parameter on the **base** streaming path (`modeling_minicpmo.py:1805/1966`) but is **not exposed** by the `MiniCPMODuplex` wrapper the harness uses (line 3129). There may be a latent native foreground think-channel the duplex wrapper discards — probe in §10.

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

## 7. Invariant compliance under turn-free operation

The conceptual move that makes this shippable in *this* harness: **a "turn" stops being a runtime control unit and becomes an audit-time label.**

| Invariant | Turn-free realization |
|---|---|
| #1 No unlogged behavior | Per-chunk decisions logged; "turns" reconstructed at audit time from the continuous stream |
| #2 No direct Thinker speech | Background model injects thoughts into KV (context only); foreground speak tokens remain gated downstream |
| #4 No proactive speech without policy approval | SpeakPolicy becomes a **continuous per-chunk gate** on the audio stream; every chunk of speak tokens passes it |
| #5 Deterministic replay | Log the model's per-chunk `(is_listen, tokens)` as the replay-safe signal; given that sequence the policy gate is bit-identical. Model sampling is non-deterministic; Tier-B replays the *recorded* chunk outputs |
| #8 Silence wins ties | `listen_prob_scale > 1` + policy gate defaults closed + native `is_listen` default |
| #10 EventLogger async non-blocking | Unchanged |

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
| Bias toward silence | `listen_prob_scale > 1` |
| Startup patience | `force_listen_count` |
| Finer granularity (~200 ms) | `chunk_ms` ↓ from 1000 |
| Unbounded session memory | `sliding_window_mode="context"` (REQUIRED once per-turn resets are gone) |
| Safety-net stop | `break_event` (VAD fast path) |
| Unified reasoning context | `llm_past_key_values` (audio/tts encoders have separate component caches — §4) |
| Logical role partition | harness-side role tags on `<unit>` blocks + role-aware `context` window (§4.1) |
| Native foreground thinking | `enable_thinking` (base streaming path only; not in the duplex wrapper — §4.1) |

## 10. Staging (probe-gated)

1. **Probe** (`scripts/probe_turn_gate_barge_in.py`, `_v2.py`) — §3 go/no-go. **DONE → GO** (§3.1). Decided: orchestration, not fine-tuning.
1b. **Backchannel-discrimination probe** — the untested half of §3.1 caveat 3. Feed a backchannel ("mm-hmm", "yeah") as the condition audio with the gate open; expect the model to KEEP speaking (no yield). Only when this passes can the model's yield judgment be trusted without the BackchannelClassifier. Until then, keep the discrimination layer (§6). Run before Stage 3.
1c. **Prompt-compliance probe** — does the duplex model honor a structured-output system prompt (treat `[CONTEXT: …]` as silent information, not speech) under streaming decode? Gates whether `background-think` (§4.1) works via in-context protocol or needs finetuning. Run before Stage 5.
1d. **`enable_thinking` re-exposure probe** — patch the duplex wrapper to pass `enable_thinking=True` to the base streaming path (§4.1); measure whether a native foreground think-channel appears and its latency cost. Informs Stage 5's think-source choice.
2. **Continuous feeder + sliding window.** Replace per-response drain; enable `context` window. (Inseparable — §5.)
3. **Model-native barge-in primary; VAD demoted to safety net.** Wire the relaxed gate's `<|listen|>` as the primary barge-in signal.
4. **SpeakPolicy → continuous per-chunk gate.** The big refactor; preserves invariants #4/#5.
5. **Background-thought injection + role-typed KV management** (§4.1) — inject thoughts as role-tagged `background-think` units via `streaming_prefill(text_list=...)`; enable role-aware eviction (Strategy 2) and the fuller role snapshot (Strategy 3). Think-source (in-context protocol vs `enable_thinking`) decided by probes 1c/1d; finetune only if 1c fails.
6. **Tune `chunk_ms` → ~200 ms and `listen_prob_scale`** empirically against Stage 6 `false_proactive_utterances_per_hour` and barge-in latency gates.

## 11. Risks

| Risk | Mitigation |
|---|---|
| Relaxing the gate degrades coherence (model trained with turn structure) | §3 probe is exactly this test; gate stages 2–6 behind a GO |
| Mid-turn `<|listen|>` poorly calibrated | `listen_prob_scale` tuning; if insufficient, fine-tune (NO-GO branch) |
| Sliding-window eviction artifacts in long sessions | TML's named open problem; measure with cost telemetry; pick `context` over `basic` |
| Determinism under continuous operation | Log per-chunk `(is_listen, tokens)`; Tier-B replays recorded outputs, not live sampling |
| Continuous SpeakPolicy is a core-path refactor | Stage 4 isolated; keep rule-based + per-chunk deterministic |
| GPU cost of continuous proposers + foreground + vision | Confirm on b200 before flag flip; the background reasoner runs off the realtime path |
| `background-think` role unreliable via prompt-only (needs finetuning) | Probe 1c (§10) decides; only `background-think` is non-native — user-audio/vision/model-speech are trained-in (§4.1). Finetune is the fallback, not the default |
| Audio-encoder KV auto-reset (~1500 cap) drops listening context mid-session | Expected behavior (§4); long-term listen memory lives in backbone KV / MemoryManager, not the audio cache |

## 12. References

- `docs/architecture-v0.1.md` — frozen spec; Parts 2 (invariants), 5 (`ThinkerProposal`), 10 (not-Moshi/not-TML).
- `docs/design-parallel-proposers-sync-talk-think.md` — predecessor; this doc supersedes its join-at-policy framing with single-KV injection.
- `docs/model-stack.md` — adapter inventory.
- `scripts/probe_turn_gate_barge_in.py` — the §3 go/no-go probe.
- Thinking Machines Lab, "Interaction Models," *Connectionism*, May 2026 — continuous micro-turns, dual-system, the long-session/robustness open questions.
- arxiv 2605.12460 (Su, Yang, Li, Geiping, "Multi-Stream LLMs") — motivating reference for true same-pass simultaneity; **verify against the PDF before final citation.**
- `modeling_minicpmo.py` (HF revision `44151b3…` for MiniCPM-o-4_5) — `streaming_generate`/`streaming_prefill`/`as_duplex`; the gate at line 3215. Line numbers are revision-specific.
