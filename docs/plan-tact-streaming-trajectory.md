# Plan — TACT-Bench streaming-trajectory harness rewrite

Status: **DRAFT (pre-codex)**. Date: 2026-05-24. Owner: this session.
Target worktree: `cah-tact-bench-vvp` (branch off `feat/tact-bench-held-result-eval`).
Cases source of truth: `tact-bench` master `7623691` (32 cases, TC1–TC32).

---

## 0. One-paragraph goal

Replace the current **Mode-A isolated per-tick probe** harness with a **real chatting-scenario streaming simulation**: feed each case's actual multi-sentence dialogue continuously (text or audio), let each model run on its **own native clock**, inject the held result at `t_available`, capture the model's **full per-tick response trajectory**, and score that trajectory mechanically against the Layer-3 ground truth. Run the same **4 arms** (vanilla / prompted / monitor_stream / audio) on **MiniCPM-o-4_5** and **gpt-realtime-2**, and emit an **HTML** comparison report.

**Success criterion (whole effort):** `run_tact_stream.py` produces, for both models × 4 arms × the 32-case bank, a per-item trajectory + a mechanical score table (cost-weighted action accuracy headline + slices + form + arbitration), and `compare_tact_stream.py` renders an HTML report — with the headline computed **without any LLM judge**, reproducible across re-runs given the same model outputs.

---

## 1. Why the current harness is wrong for this

`tact_bench_layer3_run.py` (`elicit_decisions`) does three things that defeat the goal:

1. **Leaks the labels.** `_situational_probe` literally prints `"(urgency: high; relevance: low; the user earlier asked: no)"` and §3.3 fields into the probe. Master's new **judge-from-content** contract (design §3, layer3 header) forbids this — the model must infer urgency/relevance/standing/§3.3 from content. **Hard regression vs master.**
2. **Externalizes the clock.** Every probe says `"Right now (second {t}) the user is speaking"`. The user wants to *eliminate the clock's influence* and measure the model's **native** temporal ability.
3. **Discards the real dialogue.** Each tick is a fresh stateless `model.chat()` on a synthetic per-tick string; the actual `user_script` multi-sentence turns are never streamed and there is no cross-tick continuity.

The scorer (`tact_bench_layer3.py`) is sound but **incomplete vs master §5**: it has action_accuracy / form / cried_wolf / urgent_miss, but **no cost-weighting, breakpoint-hit, T-F1/dead-time, ARS, or arbitration**.

The 32-case bank in `tact_bench_data/cases/` is the **old 28-case** copy; it must be re-synced.

---

## 2. Decision — the recommended scoring mechanism (Q1)

The user deferred this to me ("what's recommended for a benchmark"). Recommendation:

> **Run Mode-B-style execution (native stream, real responses), but score with a deterministic, mechanically-reproducible event ledger: every model emission is timestamped, matched to the ground-truth windows, and costed — the per-tick "spoke?" signal comes from the model's own native speak/silent gate and "delivered what?" from a deterministic delivery detector. No LLM judge in the headline.**

Rationale:
- A benchmark's first requirement is **reproducibility / comparability**. An LLM judge adds nondeterminism, judge-version drift, and a validation burden. Master's own design names **Mode A the "primary scored instrument"** and relegates **Mode B + judge to a "realism check"** whose metrics are "gated on judge reliability"; realization-faithfulness is explicitly a *separate judged table*, not the headline.
- The user's goal (native clock, real responses) needs the model to actually run its native loop and really speak. We reconcile by reading the per-tick **action from the model's own native signal**, which is both the *most native possible* reading and fully deterministic given the model's outputs:
  - **MiniCPM:** `is_listen` from `streaming_generate` (already surfaced by `stream_chunks`). `is_listen=False` ⇒ the model chose to speak this chunk.
  - **gpt-realtime:** a server `response`/audio onset within a tick window ⇒ spoke this tick.
  - **NOW** = spoke *and the utterance references the held payload*; **WAIT** = silent, or spoke without delivering; **DROP** = never delivered by episode end.
- The one semantic step — *did this utterance actually deliver item X, and in what form* — is done by a **deterministic delivery detector** (lexical + embedding overlap of the utterance against the held payload, fixed threshold; form via length/earcon rules). Reproducible, no LLM. This also resolves the **vanilla-chatter problem**: a chatty reply that does not reference the payload is *not* a delivery (the item is still WAITing), so free conversation does not inflate NOW.
- The **monitor_stream** arm additionally emits a parseable `<monitor>` decision, giving an **independent second read** that cross-checks the gate+detector for free.
- **Validation (advisory, non-headline):** optionally run an LLM judge on a stratified subset *only to certify the deterministic detector's agreement* (mirrors design §4d / §8). Reported as a side table, never gating the headline.

Net: real multi-sentence input ✓, native clock ✓, real responses captured ✓, mechanically reproducible headline ✓, consistent with master's design ✓.

**Failure modes this mechanism must defend against (each handled where noted):**
1. *Circularity / gaming.* NOW is **not** "the gate fired" alone — it is "gate fired **and** the utterance is matched by the detector to the held payload's window." A model that speaks every tick scores cried-wolf/interaction-cost, not free NOWs (§4.6 ledger).
2. *Mute model / `silence==WAIT` conflation.* Staying silent must NOT look like correct holding when the item should have surfaced. The end-of-episode DROP is costed with urgency-conditioned weight, and we report `never_spoke_rate` / `no_delivery_rate`, plus an **always-silent baseline** that the cost matrix must rank clearly below any useful model (§3 baselines, §4.6).
3. *Spoke-but-didn't-deliver (chatter).* Scored on a **separate `interaction_cost` axis** (non-delivery speech during dead-time or over user speech), so vanilla chatter is penalized rather than ignored (§4.6).
4. *Cross-model "spoke" is not the same measurement.* MiniCPM `is_listen` (per-chunk) vs gpt-realtime response/audio onset (VAD + buffering + latency) are reconciled by a **provider-neutral tick-attribution rule** based on wall-clock relative to user-stream start, recording both `onset_tick` and `delivery_text_tick` (§4.6).
5. *Detector threshold sensitivity.* A genuine validity risk, not a footnote: detector config (embedding model id/hash, thresholds, stopwords) is pinned in `meta.json`, fails closed if the embedding model is missing (unless `--lexical-only`), lexical-only runs reported separately, with paraphrase/negation/coincidence fixtures (§4.7).

These are the substance of the codex review; the precise spec lives in §4.6–§4.7 and the convergence log at the end.

---

## 3. Scope & fixed parameters (from the four answers)

- **Native clock:** no explicit "it is now tick t" label; the model infers elapsed time from the stream itself.
- **gpt-realtime:** **real-time 1× streaming** over a continuous WS session (faithful native clock). Accept the wall-clock + session cost; smoke-test on ≤3 cases before the full run.
- **User side:** **scripted** — play the fixed `user_script`; the user does not react to the model (keeps the authored `user_state` timeline the GT depends on).
- **Cases:** all **32**. Video-gated cases (TC6, TC23, TC28) and audio-gated TC29 fall back to master's text `[context:]` cue; flag them as under-represented (master says so itself). Arbitration cases TC30–32 included.
- **Baselines (always reported alongside the models):** `oracle` (gt-perfect, ceiling), `always-silent` (never deliver — must score clearly worst on urgent-miss/DROP cost; if it is competitive the cost matrix is mis-weighted), `always-deliver-at-availability` (NOW at every `t_avail` — the cried-wolf floor). These bracket the headline so a number is interpretable.
- **Tick tolerance:** the onset→tick bucket tolerance (default ±200ms, invariant 6) is **configurable and recorded in `meta.json`**, not hard-coded.
- **Repetition for stability (user directive 2026-05-24):** run each case × arm **k times**, persist every run, and report **mean ± spread**; draw conclusions only from effects that exceed the spread (as in the prior MiniCPM-vs-GPT report). gpt-realtime **k ≥ 3** (no seed, temp-clamped); MiniCPM decode is near-deterministic so k=1 is the floor but k>1 is allowed where the native loop shows run-to-run variance. The HTML report shows per-metric mean ± (max−min).
- **Plan review:** `/codex` (per 2026-05-24 user instruction; run via `codex exec`, content piped on stdin since the bwrap sandbox can't initialize in this env).

---

## 4. Architecture

```
tact-bench master cases  ──sync──▶  tact_bench_data/cases/{layer2,layer3}.yaml (32)
                                          │
                          ┌───────────────┴───────────────┐
                          ▼                                ▼
            scenario_timeline.py                  (gt) tact_bench_layer3.py  (extend)
   parse user_script → per-tick plan:                cost matrix + slices + arbitration
   - text chunk per tick (text arms)
   - TTS audio per tick (audio arm)                          ▲
   - pauses / idle = empty ticks                             │ trajectory → metrics
   - inject [PENDING: payload] at t_avail            ┌───────┴────────┐
                          │                          │ delivery_detector│ (deterministic)
          ┌───────────────┴───────────────┐         └───────┬────────┘
          ▼                                ▼                 │ per-tick (spoke?, text)
  minicpm_stream_runner            gpt_stream_runner ────────┘
  (reuse stream_chunks/is_listen)  (NEW continuous WS session, real-time)
                          │                                │
                          └──────────────┬─────────────────┘
                                         ▼
                         run_tact_stream.py  → trajectories.json + scores.json + meta.json
                                         ▼
                         compare_tact_stream.py → report.html
```

### 4.1 Data sync
Copy `cases/layer2-semistructured.yaml` + `cases/layer3-formal-trajectories.yaml` (and `scenarios.yaml` if we want the long-form turns) from `tact-bench` master into `tact_bench_data/cases/`. Update the scorer's case count expectations. **Note:** Layer-3 `user_state` now includes **`h` (held pause)** (TC17) — `_expand_runlength` already passes unknown tags through, but `_STATE_WORD` and any state→cost mapping must handle `h` (treat as *not a seam*, like `m` for delivery eligibility).

### 4.2 Scenario timeline builder (`scenario_timeline.py`, new)
Input: a Layer-2 case (`user_script`, `item(s)`, topic, standing_instruction). Output: an ordered per-tick plan of length `ticks`:
- `{t, kind: speech, text, audio?}` — a user turn (audio rendered via Kokoro for the audio arm).
- `{t, kind: pause}` / `{t, kind: idle}` — silence (audio = 1s zeros; text = nothing).
- `{t, kind: context, text}` — non-verbal `[context:]` cue rendered as text (perception / bystander / prosody / screen). For audio arm, contexts that master marks `modality_gated: video` have no audio equivalent → still injected as a side text note (flagged).
- Held item injected at `t_available` as a **side note** to the model's context — *not* part of the user audio/transcript. **Provider-neutral, standardized wording, byte-identical across models and arms**, carrying only what master's judge-from-content contract permits (`source · payload · t_available`) and **never** urgency/relevance/standing/staleness/§3.3 hints: `[PENDING — from {source}: {payload}]`. The exact template string is pinned in `meta.json`. The model only decides whether to surface it.

The builder is **model-agnostic** and **deterministic** (TTS cached by text hash). It is the single place that turns a case into a runnable timeline; both runners consume it.

### 4.3 MiniCPM streaming runner (`minicpm_stream_runner.py`, new; reuse existing primitives)
- Drive `MiniCPMStreamingModel.stream_chunks(audio_in queue)` — it already yields `(is_listen, text, kv_len, evt_id)` per 1s chunk. Feed the timeline:
  - **audio arm:** push the per-tick PCM (TTS'd user turns + silence) onto `audio_in`.
  - **text arms:** MiniCPM is natively audio; for a *text* input path use the per-tick text via `streaming_prefill` text segments (verify API) OR TTS the text too and treat the modality difference as the arm contrast. **Open question 4.3a — confirm MiniCPM accepts mid-stream text segments; if not, the "text arms" for MiniCPM are realized by TTS (i.e. text arms differ from the audio arm only by prompt, not modality).**
- **Held-item injection mid-session:** the held note must enter context *at* `t_available`, not at `prepare()`. `infer_stream` comments say MiniCPM "does not support mid-session re-prepare". **Open question 4.3b (BLOCKER) — how to inject `[PENDING:…]` at t_avail in as_duplex.** Candidate mechanisms to probe: (i) `streaming_prefill` with a text/system segment at that chunk; (ii) a control side-channel; (iii) restart-with-prefix at t_avail (loses KV continuity — last resort). **Must be resolved by a probe before building the runner.**
- Per-arm system prompt via `duplex.prepare(prefix_system_prompt=...)` using the arm's policy text.
- **monitor_stream arm:** prompt the model to emit `<monitor>…</monitor><speak>…</speak>` in its per-chunk text; parse `<monitor>` for the decision (reuse `_parse_monitor`), route only `<speak>` to the detector. Smoke-test for vocalization-leak / stall (design risk).
- Output: per-tick `(spoke, text)` → fed to the delivery detector → per-item trajectory.

### 4.4 gpt-realtime continuous-session runner (`gpt_stream_runner.py`, NEW — net new adapter)
The existing adapter is **out-of-band stateless only**. New continuous-session transport needed:
- Open one WS session; `session.update` with the arm's instructions + audio `output_modalities` + turn-detection config.
- **Real-time 1×:** stream the user audio via `input_audio_buffer.append` in 1s wall-clock-paced chunks (silence for pauses) so the model experiences elapsed time natively.
- **Deterministic commit policy (codex MINOR-2):** one explicit policy per run — append at a fixed 1s cadence, `input_audio_buffer.commit` on scripted turn boundaries (pauses rendered as audio silence, not commits). **Record every append/commit/response timestamp** so tick attribution (§4.6) is reconstructable; do not rely on opaque server-VAD timing for the headline.
- **Held-item injection at t_avail:** `conversation.item.create` with the standardized neutral `[PENDING …]` note (§4.2) at the tick window, out-of-band from the user audio.
- Read decisions from server events: response/audio onset ⇒ `spoke`; collect the response transcript for the detector → `delivery_text_tick`. Bucket onset wall-clock → tick (configurable tolerance, §3).
- Determinism: no seed, temp clamped → repeat runs + spread (as before).

**monitor_stream is NOT a native gpt arm (codex B4 — promoted from caveat to design).** A silent per-tick `<monitor>` text channel doesn't exist in an audio-first realtime session; forcing a text-output session changes the response channel, turn-taking, and latency — it is a *different experimental condition*, not "the same arm." Decision: the **native streaming headline for gpt-realtime is 3 arms** (vanilla / prompted / audio); `monitor_stream` is run for **both** models only as a **separate, explicitly-labeled non-native "text-control" condition** (out-of-band per-tick `<monitor>/<speak>`, reusing the existing stateless adapter) and reported in its own section, never mixed into the native cross-model headline.

### 4.5 Arms × model matrix — named by **actual input path** (codex MINOR-1)
Conditions are labeled by what really happens, not by the aspirational name, because MiniCPM text arms may resolve to TTS (open question 4.3a). The user's "4 arms" map as:

The `audio` condition = `prompted` policy with **audio input**; its *only* distinction from `prompted` is input modality, so it is a real arm **only if a text-input native path exists to contrast against** (codex round-2). Per model:

| Model | Native input | `vanilla` | `prompted` | `audio` |
|---|---|---|---|---|
| MiniCPM | audio duplex; **text-segment CONFIRMED** via `streaming_prefill(text_list=[...])` (S0a) | generic | policy, text-seg input | policy, **audio** input — distinct (text-seg works) |
| gpt-realtime | **audio only** (realtime API) | generic, audio | policy, **audio** | — **identical to `prompted`; NOT run as a separate native arm** |

**Cross-model caveat (resolved in §9):** MiniCPM's `vanilla`/`prompted` default to *text* input while gpt's are *audio* — so the clean cross-model native comparison runs on the **AUDIO** modality for both; see the §9 S0-freeze arm decision.

So: **gpt native headline = {vanilla, prompted}** (its `prompted` *is* audio-input, gpt's only native mode). The text↔audio modality penalty for gpt is reported **separately** as native-audio `prompted` vs the **non-native out-of-band-text** `prompted` (a cross-mode diagnostic, not a native arm). **MiniCPM native headline = {vanilla, prompted, audio}** when text-segment input works, else {vanilla, prompted} collapsed + renamed `*_tts`. The **cross-model native comparison runs on {vanilla, prompted}**, which both models share; `audio` is a MiniCPM-internal modality result. The exact per-model arm set is frozen at **S0-freeze** (§5) once 4.3a is known.

**Non-native text-control** (separate section, NOT in the native headline — codex B4):
| Condition | Prompt | Input path | Decision read |
|---|---|---|---|
| `monitor_stream` | `<monitor>/<speak>` | per-tick out-of-band text (both models) | parse `<monitor>` (+ gate cross-check on MiniCPM) |

Prompts: reuse master's pilot-protocol texts verbatim from `_ARMS` so they match the published experiment.

### 4.6 Trajectory scorer — the event ledger (rewrite the scoring core of `tact_bench_layer3.py`)

Every metric derives from **one matched ledger**, so an early-then-correct delivery can't be double-charged across slices (codex MAJOR-5).

**Step 1 — Emission events.** A run yields `[{onset_tick, delivery_text_tick, text, spoke}]`. `spoke` from the native gate. Provider-neutral tick attribution (codex MAJOR-1): `onset_tick = floor((onset_wall − stream_start_wall)/tick_dur)`; for MiniCPM the `is_listen=False` chunk index *is* `onset_tick`. `delivery_text_tick` = first tick whose text the detector (§4.7) matches to an item payload — distinct from mere response onset.

**Step 2 — GT windows.** Per item from Layer-3: `t_avail`, `decisive_tick`, `stale`, expected action+form, `+RA/+INT`, and the acceptable-delivery window (design §3.2): urgent `[t_avail, t_avail+δ_urgent]`; deferred `[first b-tick ≥ t_avail, stale]`; DROP = empty window.

**Step 3 — Match.** Greedily match each detector-positive emission to the item it references within tolerance. Per-item outcome: `correct` / `wrong_form` / `cried_wolf` (matched before window or DROP item) / `miss` (window closed unmatched, or matched past `stale`) / in-window-but-off-decisive → `early`/`late` (→ ARS). Unmatched detector-positive emissions = dead-time false positives. Spoke-without-match is **not** an item outcome — it feeds Step 5.

**Step 4 — Costs & headline.** Per-item outcome cost from the **pre-registered matrix** (drop-urgent 5 ≫ cried-wolf 3 ≫ wrong-form 1 ≫ early/late-defer 0.5 ≫ correct 0), DROP-of-urgent on the urgency-conditioned weight so a mute model is obviously bad (codex B3). **Headline `cost_weighted_score` = `1 − Σ item_cost / Σ worst_case_item_cost`** summed over all items across all cases (length/item-count neutral), reported next to the §3 baselines (codex MAJOR-4). Cost basis is **per-item-outcome** (primary); a per-tick-action cost is a secondary diagnostic only.

**Step 5 — Slices (lenses on the same ledger):**
- `cried_wolf` = (FP + premature) ÷ deliveries; `urgent_miss` = urgent missed ÷ urgent.
- `breakpoint_hit` = of deferred-then-delivered items, fraction whose delivery tick is a `b` state.
- `T_F1 / dead_time` = F1(delivery ticks vs acceptable windows); emissions in empty/dead windows = FP — computed from the ledger, no separate pass.
- `ARS` = Σ urgency-conditioned asymmetric penalty for in-window deliveries: early slope `w_early·(t_lo−t)`, late slope `w_late·(t−t_hi)`, urgent → `w_late ≫ w_early`, low-value → `w_early ≥ w_late`; capped at drop cost; **past `stale` = `miss`, not late** (codex MAJOR-6). Slopes/units/`δ_urgent` pre-registered in `meta.json`.
- `interaction_cost` (new axis, codex MAJOR-3 / MINOR-6) = non-delivery speech ticks overlapping user `m`/`h` or dead-time ÷ such ticks — penalizes vanilla chatter / talking over the user that the delivery axis ignores.
- `never_spoke_rate`, `no_delivery_rate` (codex B3).

**State-semantics table (codex MAJOR-7) — authoritative in the scorer, not just `_STATE_WORD`:**
| state | user speaking | breakpoint? | deliverable (non-urgent)? | dead-time? | cost mod |
|---|---|---|---|---|---|
| `m` mid-utterance | yes | no | no | no | high interrupt |
| `h` held pause | yes (thinking) | **no** | **no** (TC17: not a seam) | no | high interrupt |
| `b` breakpoint | no | **yes** | yes | no | low |
| `i` idle | no | no | yes (lowest) | yes (FP target) | lowest |

**Suffixes (codex MAJOR-8):** `parse_action` keeps `+RA`/`+INT`. `+RA` (TC14): a delivery the detector finds doesn't re-anchor the earlier ask → partial (`wrong_form`-class). `+INT`: delivering during `m`/`h` is not charged interrupt for that item. Add parser tests.

**Arbitration (codex B5) — explicit per-tick multi-item policy.** Eligible = `t_avail ≤ t`, non-terminal, not superseded, not suppressed. Single channel: ≤1 delivery/tick; order by `harm_severity` desc → urgency → `t_avail` → waited-longest (design §3.2/§3.3). Violations: (a) lower-priority delivered while a higher-priority item is in-window; (b) superseded item delivered after its superseder is available (TC8/TC32); (c) duplicate delivery; (d) delivery before window opens. `arbitration_accuracy` = multi-item cases (TC8/30/31/32) with zero violations ÷ multi-item cases; duplicate/superseded deliveries also cost as cried-wolf.

**Keys (codex NIT-2):** keep `legacy_action_accuracy` (old binary) for the old report; the new headline is `cost_weighted_score`. No name collision.

### 4.7 Delivery detector (`delivery_detector.py`, new, deterministic) — treated as a validity surface, not a footnote (codex MAJOR-2)
`detect(utterance_text, item_payload, earlier_ask) -> (delivered, form, reanchored, confidence)`:
- **delivered** = lexical overlap (content-word token-set F1) **or** embedding cosine ≥ threshold vs payload; both thresholds **pre-registered and written to `meta.json`** (codex NIT-3/NIT-4).
- **embedding model pinned by name + revision hash in `meta.json`; fail closed** if it's unavailable — the run aborts unless `--lexical-only` is passed, and lexical-only runs are tagged and **reported as a separate column**, never merged with embedding runs (codex MAJOR-2/MINOR-4).
- **form** from the channel/contract first: `CHIME`/`SILENT_NOTIFY` only if a non-verbal marker/channel was used; `SPEAK_FULL` vs `SPEAK_BRIEF` decided by a deterministic **payload-slot** rule (did the utterance include the full requested detail?) with token length only as a tiebreak fallback (codex MINOR-4).
- **reanchored** (for `+RA` items): does the utterance reference the *earlier ask* (`earlier_ask` from the case)? Drives the `+RA` partial-credit rule in §4.6.
- Pure function, no network. **Fixture tests (mandatory):** true paraphrase delivery (positive), negation ("not done yet") (negative), coincidental keyword overlap (negative), verbose unrelated chatter (negative), brief correct delivery (positive). These gate S2.

### 4.8 Report (`compare_tact_stream.py`, adapt `compare_tact_layer3.py`)
HTML with:
- **Header banner caveats (not buried — codex NIT-5/B3):** "GT is v1, pending human validation"; the 3 baselines (oracle / always-silent / always-deliver) printed in the headline table so the reader sees the scale; lexical-only vs embedding-detector flagged.
- Arm setup (reuse `_setup_md`), per-case natural descriptions (reuse `_load_descriptions`).
- Headline `cost_weighted_score` + slices table: native-headline conditions (vanilla/prompted/audio × both models, mean ± spread for gpt) + a **separate** non-native `monitor_stream` section.
- Per-case **trajectory ribbons** (gt vs each condition) AND **expandable per-tick audit rows (codex MINOR-3):** `tick · spoke · text · detected_item · confidence · gt_state · assigned_cost` — so every mechanical score traces back to emitted text + detector result.
- Methods/caveats: judge-from-content, native-clock (strongest on `audio`), detector limitation + threshold config, video-gated under-representation (TC6/23/28, TC29), interaction-cost axis. Output `report.html`.

---

## 5. Staging into subplans (each converges via plan-critic before its build)

**S0 is a hard design-freeze gate (codex MAJOR-9).** Nothing downstream starts until S0 produces a written outcome that fixes the model×arm matrix, the held-item injection mechanism, and headline eligibility — because any S0 result can reshape the scoring surface and report dimensions.

| # | Subplan | Success gate | Depends |
|---|---|---|---|
| S0a | **Probe:** MiniCPM mid-session `[PENDING]` injection in as_duplex (4.3b) | injection lands; model can reference it after t_avail and not before | — |
| S0b | **Probe:** gpt-realtime continuous real-time round-trip (append audio + commit + item.create + read response onset/transcript) | one tick round-trips; timestamps recorded | — |
| S0c | **Cost dry-run (codex MINOR-5):** from S0b's measured session duration + token/audio usage, project wall-clock + $ for 32 × native-arms × k | a go/no-go budget number for the user | S0b |
| **S0-freeze** | **Write the frozen matrix + injection decision + headline eligibility** | doc updated; user checkpoint | S0a,S0b,S0c |
| S1 | **Data sync + scorer event-ledger** | 32 cases load (`h` handled); ledger metrics computed; **oracle ≈ ceiling, always-silent ≪ oracle, always-deliver high cried-wolf** (baseline sanity) | S0-freeze |
| S2 | **Scenario timeline builder + delivery detector** | case → correct per-tick plan; detector passes the §4.7 paraphrase/negation/coincidence fixtures | S1 |
| S3 | **MiniCPM streaming runner** | one case end-to-end on b200 → ledger; monitor-leak smoke-test | S2 |
| S4 | **gpt-realtime streaming runner** | one case end-to-end real-time → ledger | S2 |
| S5 | **`run_tact_stream.py` orchestration** (both models, native arms + non-native monitor, persistence) | full suite → trajectories + scores + meta | S3,S4 |
| S6 | **HTML report** | renders native headline + separate monitor section + audit rows + caveats | S5 |

---

## 6. Risks & open questions (for codex)

- **R1 (BLOCKER, 4.3b):** MiniCPM as_duplex mid-session held-item injection mechanism is unconfirmed. Mitigation: S0a probe first.
- **R2 (4.4a) — RESOLVED in §4.4/§4.5:** monitor_stream is a different experimental condition for an audio-first model, so it is pulled out of the native headline and run as a separate, explicitly-labeled non-native text-control condition for both models.
- **R8 (cross-model "spoke" comparability, codex MAJOR-1) — mitigated in §4.6:** `is_listen` (MiniCPM) and response onset (gpt) are not the same measurement; canonical tick attribution uses wall-clock relative to user-stream start and records both `onset_tick` and `delivery_text_tick`. Residual risk: transport latency differences; only normalize if the S0b probe measures it consistently.
- **R3 (cost/time):** real-time 1× gpt over 32 cases × 4 arms × k runs is slow and holds a paid session. Mitigation: smoke ≤3 cases; estimate wall-clock + $ before full run; consider running gpt arms overnight / in background.
- **R4 (detector validity):** deterministic detector mislabels paraphrase/coincidence. Mitigation: embedding overlap + report judge-agreement on a validation subset (advisory).
- **R5 (text "native clock"):** a text model has no real clock; text arms approximate it via per-tick chunk arrival (no explicit label). The clean native-clock claim is strongest on the **audio** arm; state this in the report.
- **R6 (modality_gated):** TC6/23/28 (video), TC29 (audio) under-represented; text/context fallback only. Flag, don't silently score.
- **R7 (GT is v1):** Layer-3 trajectories are pending human validation; headline reported with that caveat.
- **Q (process):** after codex converges this, confirm whether to build all of S0–S6 autonomously (autonomous-execution-mode) or checkpoint with the user after S0.

---

## 7. Out of scope (v1 of this rewrite)
- Reactive (LLM-driven) user.
- Video modality.
- Tier-2/Tier-3 (floor-reading / joint) — Tier-1 oracle-floor only.
- Calibration/ECE.
- Real-session-grounded (§4b) cases.

---

## 8. Codex convergence log

**Round 1** (`codex exec`, gpt-5.5, read-only via stdin — the bwrap sandbox couldn't initialize in this env so content was piped). Findings → resolutions:

| Codex finding | Severity | Resolution |
|---|---|---|
| Mute model / `silence==WAIT` conflation | BLOCKER | §2 fm-2, §3 baselines, §4.6 Step-4 urgency-conditioned DROP cost + `never_spoke_rate`/`no_delivery_rate` + always-silent baseline |
| gpt monitor_stream is a different modality/task | BLOCKER | §4.4/§4.5 — pulled out of native headline; separate non-native text-control condition |
| Arbitration underspecified | BLOCKER | §4.6 explicit per-tick multi-item policy + violation taxonomy |
| Cross-model "spoke" comparability | MAJOR-1 | §4.6 provider-neutral tick attribution; R8 |
| Detector threshold sensitivity = validity risk | MAJOR-2 | §4.7 pin embedding+hash, fail-closed, lexical-only separate, fixtures |
| Spoke-but-didn't-deliver needs cost | MAJOR-3 | §4.6 `interaction_cost` axis |
| Cost normalization/denominator | MAJOR-4 | §4.6 per-item-outcome basis + normalized `cost_weighted_score` + baselines |
| T-F1 vs cost double-counting | MAJOR-5 | §4.6 single event ledger |
| ARS not operationalized | MAJOR-6 | §4.6 slopes/zero/urgency-mult/cap/stale=miss, pinned in meta |
| Breakpoint/`h`/state semantics | MAJOR-7 | §4.6 state-semantics table |
| `+RA`/`+INT` suffixes missing | MAJOR-8 | §4.6 suffix rules + §4.7 reanchor detection + parser tests |
| S0 must freeze design before S1 | MAJOR-9 | §5 S0-freeze hard gate |
| `[PENDING]` may be a label artifact | MAJOR-10 | §4.2 neutral standardized schema, no label hints |
| MiniCPM text arms aren't text if TTS | MINOR-1 | §4.5 named by actual input path |
| gpt commit policy ambiguous | MINOR-2 | §4.4 deterministic append/commit policy + timestamps |
| Report needs audit links | MINOR-3 | §4.8 per-tick audit rows |
| Form via length too crude | MINOR-4 | §4.7 form from channel/payload-slot, length fallback |
| gpt cost dry-run before full suite | MINOR-5 | §5 S0c |
| User non-reactivity unscored | MINOR-6 | §4.6 `interaction_cost` (overlap with scripted user) |
| "Mode-A-grade" overclaim | NIT-1 | §2 → "mechanically reproducible" |
| old/new key collision | NIT-2 | §4.6 `legacy_action_accuracy` vs `cost_weighted_score` |
| ±200ms should be config | NIT-3 | §3 configurable + meta |
| embedding model vague | NIT-4 | §4.7 pinned id+hash |
| GT-v1 caveat prominence | NIT-5 | §4.8 header banner |

**Round 2** (`codex exec`, convergence check): "no round-1 finding materially unresolved." One new catch — the gpt native arm matrix was internally inconsistent (`prompted` and `audio` are the same audio-input session for gpt). Verdict: **CONVERGED for S0 probes; the gpt prompted/audio duplication must be resolved before S0-freeze.** → Fixed in §4.5 (gpt native = {vanilla, prompted}; `audio` is MiniCPM-only; modality penalty for gpt reported as native-audio vs non-native-text). 

**Status: CONVERGED — cleared to start S0 probes.** The remaining open questions (4.3a text-segment support, 4.3b injection mechanism, S0c budget) are exactly what S0 resolves before S0-freeze.

---

## 9. S0 probe results + freeze (2026-05-24)

Both gate probes **PASS** — no design change forced.

**S0a — MiniCPM mid-session injection: VIABLE.** Mechanism: `MiniCPMODuplex.streaming_prefill(audio_waveform=…, text_list=[note])` (model rev `4382fcae`, `modeling_minicpmo.py:2759` → text branch `:3093-3116` → `decoder.feed(text_embeds)` into the persistent KV cache). Injected token appeared **only after** injection in 4/4 runs; never before. Requires the same turn-gate relaxation `stream_chunks` already uses (`current_turn_ended → True`). Resolves **4.3b**. Bonus: confirms **4.3a** — MiniCPM accepts **text** mid-stream, so its `vanilla`/`prompted` text arms are real native arms (no TTS fallback needed). Probe: `scripts/probe_minicpm_midsession_inject.py`. Finding: MiniCPM **parrots** the note on arrival despite "keep private" — capability ✓, *holding* is the policy the benchmark scores (vanilla blurts = expected baseline).

**S0b — gpt-realtime continuous session: VIABLE.** One persistent WS session takes **multiple user turns**, accepts a mid-session held note via `conversation.item.create` (role `system`), and the model references it ("…the deploy finished successfully, and the build is 2.4.1"). Real-time 1× audio (`input_audio_buffer.append` ×N + `commit` + `response.create`) round-trips; onset is timestampable via `response.created` / `response.output_text.delta` / `input_audio_buffer.speech_started`. Probe: `scripts/probe_gpt_realtime_session.py`. Findings folded into S4: (i) set adequate `max_output_tokens` (≥ ~80 reasoning headroom) or output is empty; (ii) avoid "secret" framing (privacy refusal); (iii) `turn_detection` config needs the correct GA shape (the `null` form errored).

**S0c — budget:** real-time 1× → gpt **native** suite ≈ 32 cases × ~11.5s avg × {vanilla, prompted} × k=3 ≈ **~35–45 min wall-clock**, low single-digit USD (audio-in tokens cheap); monitor_stream (out-of-band) adds API calls but no real-time wait. k tunable.

**FROZEN injection mechanisms:** MiniCPM `streaming_prefill(text_list=[PENDING…])` at `t_avail`; gpt `conversation.item.create(role=system, [PENDING…])` at `t_avail`. Both honor "arrives at t_avail" with session continuity.

**OPEN at freeze — arm matrix (needs user pick), because "4 arms on both models" and "native clocks" conflict for gpt (realtime is audio-only, no native text path):**
- **Option A (cleanest, recommended):** cross-model native headline = `{vanilla, prompted}` on **AUDIO** for both models; MiniCPM additionally runs **text-input** `{vanilla, prompted}` as a within-model modality contrast; `monitor_stream` non-native for both. Fewest confounds; honest native-clock claim.
- **Option B (full 4×2 grid):** run all 4 arms on both models, tagging gpt's text-input `vanilla`/`prompted` as **non-native out-of-band** (no native clock). Preserves the grid but gpt's text-vs-audio contrast is confounded by native-ness.

**DECISION (user, 2026-05-24): Option A.** Frozen arm set:
- **Cross-model native headline (AUDIO input):** `{vanilla, prompted}` on both MiniCPM and gpt.
- **MiniCPM-only modality contrast (TEXT input via `text_list`):** `{vanilla, prompted}`.
- **Non-native (out-of-band per-tick text), both models:** `monitor_stream`.

**Process decision:** the master plan is codex-converged (§8) and decomposed in §5; execute S1–S6 stage-by-stage with a `reviewer` pass after each coder (autonomous-execution-mode) rather than authoring six separately-critic'd subplan docs. Checkpoint with the user before the costly real-time gpt run (S5) and before committing results.

**gpt cadence decision (user, 2026-05-24): oracle-floor-timed.** gpt-realtime is reactive (no native per-chunk "choose silence" gate; a triggered response (almost) always emits). So the harness triggers a gpt response opportunity ONLY at the authored deliverable seams (floor `b`/`i`); gpt decides whether/what to surface at each, and the deterministic detector reads the result. gpt does NOT infer the floor (that self-referential floor-reading design is deferred Tier-2/3). Server VAD is disabled; the harness drives `commit` + `response.create` at seam ticks; onset tick = the seam tick. **Consequence (documented, not a bug):** gpt cannot interrupt mid-utterance, so urgent items in all-`m` cases (TC1/15/28/29/31) are *structural misses* for gpt — the honest reactive-vs-full-duplex contrast. MiniCPM keeps its native per-chunk `is_listen` gate (can interrupt any chunk; NOT seam-restricted); this cross-model asymmetry is inherent to the architectures and reported as such.
