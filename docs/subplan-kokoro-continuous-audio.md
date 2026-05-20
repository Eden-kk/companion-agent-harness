# Sub-plan: Kokoro@200 — responsive continuous voice (text@200 → Kokoro TTS)

**Status:** DRAFT (round 0) — for plan-critic.
**Stacks on:** PR6 (`port/pr6-native-continuous-audio`). NEW mode alongside native audio.
**Why:** native audio is locked to `chunk_ms=1000` (~1s latency → choppy + slow barge-in). The model's TEXT path is coherent at `chunk_ms=200` (#363 sweep). So run text@200 (responsive, fast barge-in detection) and speak it via Kokoro (fast ONNX TTS, 24 kHz = the audio-out rate). **One outcome:** a responsive `--continuous` companion (Kokoro voice) with sub-second barge-in.

## Proven foundations
- Text@200 coherent: #363 `probe_chunk_ms_sweep`. (generate_audio=False)
- The gate-relax (`_AlwaysEnded`) SUPPRESSES speech (PR6 probes v1) → must be SKIPPED so the model emits text responses.
- `KokoroTtsAdapter.synthesize(text, prosody_tags) -> AsyncIterator[bytes]` yields PCM16 @24 kHz (`tts_kokoro.py:122,148`). `AudioOutputController.push_chunk` + the broker flush (PR6) already route audio + barge-in flush.

## Design

### 1. Adapter (`foreground_model_minicpm.py`) — `external_tts` mode (text@200, no relax)
- New ctor flag `external_tts: bool = False`. When True: `as_duplex(generate_audio=False, sliding_window_mode=…, chunk_ms=chunk_ms)` (chunk_ms=200 from factory); `self._chunk_samples = _SAMPLE_RATE * chunk_ms // 1000` (3200); set `self._skip_gate_relax = True`.
- Generalize the gate-relax skip: `self._skip_gate_relax = native_audio or external_tts`; in `stream_chunks`, skip the `_AlwaysEnded` swap + restore when `self._skip_gate_relax` (so the model emits text). `prepare()` stays the plain `prefix_system_prompt` form (no prompt_wav — token2wav not used). external_tts yields the **4-tuple** `(is_listen, text, audio_kv_len, evt)` (no audio bytes from the model).
- native_audio + external_tts are mutually exclusive (assert / native takes precedence). Non-speak (neither) path unchanged.

### 2. Pipeline (`live_pipeline.py`) — Kokoro speaker
- `build_continuous_pipeline(..., tts="kokoro")`: construct `MiniCPMStreamingModel(external_tts=True, chunk_ms=200)` + a `KokoroTtsAdapter` + the same `AudioOutputController`. Pass the Kokoro adapter into `ContinuousOrchestrator(tts_adapter=...)`.

### 3. Orchestrator (`continuous_orchestrator.py`) — phrase-buffer + concurrent Kokoro speaker
- New optional `tts_adapter` ctor arg (default None = no external TTS; native/text-only paths unchanged).
- **Concurrency model (the crux):** a single background **speaker task** drains a `phrase_queue: asyncio.Queue[str]`, synthesizing one phrase at a time so audio stays ordered:
  ```python
  async def _speaker(self):
      while True:
          phrase = await self._phrase_queue.get()
          self._audio_output.start_generation(caused_by=[...])   # mark playing + clear stop
          async for pcm in self._tts_adapter.synthesize(phrase, []):
              if self._audio_output._stop_event.is_set(): break   # barge-in mid-phrase
              await self._audio_output.push_chunk(pcm, caused_by=[...])
  ```
  `run()` (model loop) does NOT block on synthesis — it only appends phrases + handles barge-in. Spawn `self._speaker_task = asyncio.create_task(self._speaker())` at the top of `run()`; cancel it in a `finally`.
- **Phrase buffering:** on a speak chunk (`not is_listen and text`), append `text` to `self._phrase_buf`. Flush the buffer to `phrase_queue` when it ends in sentence punctuation (`.?!…`) OR exceeds N chars (e.g. 60) OR on the speak→listen transition. (Keeps Kokoro phrases coherent, not per-fragment.)
- **Barge-in:** on `is_listen=True` while `audio_output.is_playing` → existing `_act` request_stop (sets `_stop_event` → speaker breaks current synth + flush fires) + drain `phrase_queue` (discard pending phrases) + clear `_phrase_buf`. (BC veto from PR3c still applies in `_act`.)
- AGENT dialogue (proposer_token_buffered) already emitted from the text — keep it.

### 4. Server (`server.py`)
- `--continuous` defaults to the Kokoro path (or add `--tts kokoro|native`). For now: continuous factory builds `external_tts=True` + Kokoro (the responsive default). Keep native reachable via a flag if cheap.

## Success criterion
- **Operator (b200):** responsive mic test — companion answers in Kokoro's voice with sub-second start + smooth playback; **talking over it stops it within ~200-400ms** (fast barge-in).
- **CPU tests** (`tests/test_continuous_kokoro.py`): (a) phrase-buffering — fragments accumulate, flush on punctuation/transition; (b) orchestrator drives a fake tts_adapter (async-gen of bytes) → push_chunk called with synthesized bytes on speak; (c) barge-in (is_listen flip) → `_stop_event` set + phrase_queue drained + no further push. Reuse fakes; no GPU.

## Risks / open
| Item | Handling |
|---|---|
| Concurrency: speaker task vs barge-in race | single speaker task + `_stop_event` check per chunk + queue drain on barge-in; cancel task in run() finally. |
| Phrase latency (wait for boundary) | flush on punctuation OR 60 chars OR transition → first audio after first short phrase (~0.6-1s), then streams. Tunable. |
| Kokoro keeps up at 200ms text rate? | Kokoro is fast (ONNX, faster-than-realtime); phrase queue absorbs bursts. Verify on deploy. |
| `start_generation` per phrase vs once | call per phrase to clear `_stop_event` + mark playing; `request_stop` on barge-in. Mirrors turn-based. |
| gate-relax skip for text mode | proven safe (PR6 probes: model emits text without relax). |

## Out of scope
CosyVoice2 (same path, swap adapter); voice selection; the merged-stack gate-relax audit; native audio (kept as an alternate mode).
