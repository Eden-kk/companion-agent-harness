# Benchmark & Experiment Results

Consolidated results for the realtime companion harness (MiniCPM-o 4.5 duplex
proposer + Kokoro ONNX TTS + faster-whisper ASR). All numbers are on the **b200**
host unless noted.

**Compiled:** 2026-05-21. **Convention:** results are tagged **[measured]** (an
observed number) or **[target]** (an acceptance gate / goal, not yet measured).
Small-sample probes are tagged with their N. Each row cites its source so the
number can be re-derived.

---

## 1. End-to-end / turn latency  [measured]

| Metric | Value | Source |
| --- | --- | --- |
| EOU → policy decision → TTS synthesis start | **4386 ms median, 15700 ms max** | `status-snapshot-2026-05-17.md` |
| EOU → first audio (Path B default) | **3–12 s** | `plan-tier2-latency-wins-v2.md` §1.2 |
| Per-turn latency growth (Mechanism-B KV, no reset) | turn 2 **4.4 s** · turn 3 **10.2 s** · turn 5 **15.7 s** (resets on session restart) | `plan-minicpm-reset-and-latency-survey.md` §0 (HEAD b5dc8ba) |

The dominant residual is the proposer running slower than real time per chunk,
plus Kokoro waiting on a full snapshot before first audio. The per-turn growth is
the KV cache accumulating across turns; it disappears on session restart, which
motivated the (still-unshipped) session-reset work.

### Per-hop baseline  [measured]

| Hop | Latency | Source |
| --- | --- | --- |
| VAD frame | ~2 ms/frame | `basic-stack-design-2026-05-16.md` §2 |
| ASR (faster-whisper) | 200–500 ms | same |
| Addressing classify | <100 ms | same |
| Policy decision | <1 ms | same |
| MiniCPM first proposal token | 200–600 ms | same |
| Kokoro synthesis (first chunk) | 1–3 s | same |

---

## 2. Per-chunk decode throughput / token density  [measured]

- **`max_new_speak_tokens_per_chunk` saturation = 70 tokens/chunk.** Sweeping
  `[20, 35, 50, 70, 100]` and tracking `density_per_ms = total_chars / p50_ms`,
  density saturates at 70; the default was raised 50 → 70.
  Source: commit `dc23510` ("tune: … density saturation point"), probe
  `scripts/probe_pathb_density_sweep_v2.py`.

---

## 3. Audio KV-cache reset coherence  [measured]

- **Continuous ~80-chunk (~80 s) native synthesis stays coherent across the
  model's internal KV auto-reset** (~1500-token ceiling); no context break
  observed. Source: `scripts/probe_audio_kv_reset.py`, confirmed in
  `status-snapshot-2026-05-17.md` §0.

---

## 4. torch.compile  [decision + measured cost]

- **Reverted / disabled for Path B** — upstream blocker PyTorch #168042 (CUDA
  graph + dynamic-shape mismatch on the duplex path). Source: commit `8050725`
  ("revert torch.compile auto-enable for Path B").
- Compile/warmup cost when enabled: **20–30 s** one-time. [measured]
- Expected steady-state speedup **1.2–1.4×** (`mode="default"`, `dynamic=True`,
  `fullgraph=False`). [target] — the earlier 1.7–2.1× claim was rejected as a
  CUDA-graph artifact. Source: `plan-tier2-latency-wins-v2.md` §2.1.

---

## 5. Native barge-in / duplex turn-gate  [experiment, this session, N=1]

**Root cause (read-only investigation).** Listen vs. speak is a token in the LLM
vocabulary (`<|listen|>`). The turn-gate at `modeling_minicpmo.py:3216` remaps a
mid-turn `<|listen|>` sample to `tts_bos` whenever `current_turn_ended == False`,
and `is_listen` is read *after* the gate (3221). So while the model is speaking it
is structurally forbidden from yielding, and in native-audio mode the harness
never sees a mid-turn intent to listen → native barge-in cannot fire.

**Probe result.** Forcing `current_turn_ended = True` per chunk (on-demand
gate-open via an `_AlwaysEnded` descriptor) makes the native model **yield on a
real interrupt at `listen_prob_scale=1.0` and ignore a backchannel ("hmm").**
Verdict: **content-aware native barge-in is FEASIBLE.** N=1, not yet shipped as a
feature. Source: `/tmp/probe_native_ondemand_gate.py`.

> Note: origin/main `#362` ships a gate-relax + stop-on-`is_listen` barge-in for
> the path where `is_listen` is exposed; the result above is specifically about
> the **native-audio** path, where the gate hides that signal.

---

## 6. Live TTS-mode toggle (one model)  [experiment, this session, N=1]

- **Flipping `duplex.generate_audio` mid-session** (native → text-only → native)
  produces coherent output in both directions with no crash or corruption.
  Verdict: a single loaded model can switch **native@1000 ↔ text@1000+Kokoro**
  live — no per-mode reload needed. This backs the `tts` hot-seam toggle.
  Source: `/tmp/probe_generate_audio_toggle.py`.

---

## 7. Model load & warmup  [measured, this session]

Integration build (`integration/tts-asr`, native + vision + Kokoro + ASR), GPU 6:

| Stage | Time |
| --- | --- |
| MiniCPM-o load | 14.6 s |
| Kokoro-82M-ONNX load | 1.67 s |
| Warmup — MiniCPM | 3986 ms |
| Warmup — Kokoro | 202 ms |
| Warmup — ASR (whisper-tiny) | 284 ms |
| Warmup — total | 4472 ms |

Vision tower adds ~18 GB VRAM (`init_vision=True`).

---

## 8. Open targets (not yet measured)

| Target | Gate | Source |
| --- | --- | --- |
| Post-KV-reset latency | median `policy→tts` ≤ 1500 ms, max stage ≤ 2500 ms, turn 5 ≤ 1.5× turn 2 | `plan-minicpm-reset-and-latency-survey.md` §1.8 |
| CosyVoice2 (Chinese) TTFT | ≤ 500 ms steady-state | `plan-cosyvoice2-tts-integration.md` |
| Speculation hit rate | ≥ 85% on multi-turn fixtures | `plan-tier2-latency-wins-v2.md` §4.2 |
| Speculative ASR | p95 < 150 ms (else disable for session) | same |

---

## Sources

- `docs/status-snapshot-2026-05-17.md`
- `docs/plan-minicpm-reset-and-latency-survey.md`
- `docs/plan-tier2-latency-wins.md`, `docs/plan-tier2-latency-wins-v2.md`
- `docs/streaming-speculative-component-survey.md`
- `docs/basic-stack-design-2026-05-16.md`
- `docs/plan-cosyvoice2-tts-integration.md`
- `scripts/probe_pathb_density_sweep_v2.py`, `scripts/probe_audio_kv_reset.py`
- `/tmp/probe_native_ondemand_gate.py`, `/tmp/probe_generate_audio_toggle.py`
- commits `dc23510` (density tune), `8050725` (torch.compile revert)
