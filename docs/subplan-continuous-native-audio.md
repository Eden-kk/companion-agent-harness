# Sub-plan: PR6 — native single-duplex audio for the continuous companion (make it speak)

**Status:** READY (round 3, converged).
**Stacks on:** `port/pr5b-continuous-wiring` (#367). NEW PR (the continuous path runs `generate_audio=False` → text decisions only, no audio).
**One outcome:** the `--continuous` companion produces **audible MiniCPM-native speech**, from a single `generate_audio=True` duplex that listens and speaks (one KV cache), with barge-in stopping speech.

## Proven recipe (empirical, b200 — `/tmp/probe_single_duplex_audio_v{1,2,3,4}.py`)
- One `as_duplex(generate_audio=True)` duplex listens AND speaks (v2/v3 produced coherent responses; v4 produced REAL 24 kHz audio — `/tmp/probe_v4_response.wav`, 2 s).
- token2wav init = `_patch_torchaudio()` (workaround for missing `torchcodec` that breaks `prepare(prompt_wav_path=…)`) + `prepare(prompt_wav_path=<silent 16k ref>)`.
- **Do NOT gate-relax:** v1 (gate-relaxed `current_turn_ended=True`) → never spoke; v2–v4 (no relax, natural turn-taking) → spoke. The relax suppresses speech. v2–v4 also confirm the loop runs **continuously across a turn** (listen→speak→listen, 30+ chunks) without the relax.
- Caveat: low volume (peak 0.069) with the silent ref → normalize / voiced ref.

## Design (concrete — critic blockers resolved)

### 1. Adapter (`foreground_model_minicpm.py`) — `native_audio` mode (one duplex)
- New ctor flag `native_audio: bool = False`. When True, in `__init__`:
  - `_patch_torchaudio()` (import from `tts_minicpm_native`); build `self._duplex = base.as_duplex(generate_audio=True, sliding_window_mode=sliding_window_mode, chunk_ms=1000)` — **native_audio forces `chunk_ms=1000`** (the probe-proven audio config; the ctor's `chunk_ms` arg is overridden to 1000 in native mode — document; `chunk_ms=200` audio is a follow-up probe);
  - write a silent 16 kHz ref (mirror `tts_minicpm_native._write_silent_ref`); store path on `self`;
  - set `self._native_audio = True`.
  - **token2wav `prepare(prompt_wav_path=ref)` happens at the start of `stream_chunks`** (where `prepare` already runs), passing `prompt_wav_path` in native mode — NOT in `__init__` (prepare resets per-session state; keep it in the existing `stream_chunks` prepare call).
- `native_audio=False` path **byte-identical** to today (zero regression for PR1–5b).
- **`infer_stream` is unsupported on a `native_audio` instance** (continuous-only; one duplex does listen+speak). One-line ctor docstring note (mirrors PR5a's chunk_ms note). The continuous deployment never calls `infer_stream`, so no collision.
- `stream_chunks`: make the `_AlwaysEnded` swap AND its `finally` restore conditional, and change the `prepare()` signature in native mode — explicit structure:
    ```python
    if not self._native_audio:
        duplex.__class__ = relaxed_cls
    try:
        if self._native_audio:
            duplex.prepare(prefix_system_prompt="...", prompt_wav_path=self._ref)
        else:
            duplex.prepare(prefix_system_prompt="...")
        ... per-chunk loop ...
    finally:
        if not self._native_audio:
            duplex.__class__ = orig_cls
    ```
    (gate-relax suppresses speech; probes v2–v4 confirm natural turn-taking runs continuously without it.) **This is the existing INNER `try/finally` in `stream_chunks`** — the conditional replaces the inner-try's current unconditional `duplex.__class__ = relaxed_cls` and the inner-`finally` restore; it nests inside the outer `try/finally` that toggles `_stream_chunks_active` (leave that outer block as-is).
- `_gpu_work` (native mode): capture `result.get("audio_waveform")`; when non-empty, convert to PCM16 bytes via `(np.clip(wav, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()` (the `tts_minicpm_native` pattern); apply a **hardcoded ×4 gain constant** (not a parameter) for the low-volume issue. Yield a **5-tuple** `(is_listen, text, audio_kv_len, audio_pcm_bytes, caused_by_evt_id)` (`audio_pcm_bytes=b""` when not speaking). Non-native mode keeps the 4-tuple.

### 2. Orchestrator (`continuous_orchestrator.py`) — backward-compatible unpack + routing
- `run()` unpacks **defensively** to tolerate BOTH arities (so the 8 existing 4-tuple test fakes are UNAFFECTED — no edits to them):
  ```python
  is_listen, text, audio_kv_len, *mid, caused_by_evt_id = record
  audio_pcm = mid[0] if mid else b""
  ```
- `_ForegroundModelProtocol`: set the exact return annotation to the union of both arities — `AsyncGenerator[tuple[bool, str, "int | None", str] | tuple[bool, str, "int | None", bytes, str], None]` (Protocol is structural; existing 4-tuple fakes still satisfy it).
- `_AudioOutputProtocol` (the existing Protocol in this file): **add `async def push_chunk(self, pcm_bytes: bytes) -> None: ...`** (alongside `is_playing`/`start_generation`/`request_stop`).
- After `decide_chunk` + `_act` (audit unchanged — #1/#4): if `audio_pcm` and the chunk is a speak chunk (`not is_listen`), `await self._audio_output.push_chunk(audio_pcm)` (the orchestrator `run()` is async). **Route through the orchestrator (not the adapter)** so the PR3c BC-veto / PR3b barge-in logic in `_act` stays authoritative over speech.
- Barge-in: existing `_act` path — `is_listen=True` while playing → `request_stop` (which now also signals push_chunk to drop) + barge-in event; BC veto suppresses as today.

### 3. Audio sink (`audio_output_controller.py`) — `push_chunk`
- Add **`async def push_chunk(self, pcm_bytes: bytes) -> None`**: `await self._sink(pcm_bytes)` — `_sink` is `Callable[[bytes], Awaitable[None]]`, invoked with `await` in `play()` (~line 169), so push_chunk MUST be async. **Respect `_stop_event`** (return early/drop if a stop is pending). The sink → broker enqueue is drop-oldest (invariant #10).

### 4. Pipeline / server
- `build_continuous_pipeline` (`live_pipeline.py`): construct `MiniCPMStreamingModel(..., native_audio=True)`. The `AudioOutputController` + `WebSocketAudioSink` → `audio_out_broker` → `/ws/audio_out` are already wired (PR5b); browser format is PCM16 @24 kHz = the model's audio rate (no resample).
- `server.py` continuous factory: pass `native_audio=True` (native_audio overrides chunk_ms to 1000 internally). Default (`--continuous` off) unchanged.

## Success criterion
- **Operator manual gate (b200):** redeploy `--continuous` → mic test: companion answers **audibly** in MiniCPM's voice; **talking over it stops its speech** (barge-in).
- **CPU tests** (new `tests/test_continuous_native_audio.py`): (a) orchestrator routes audio — a fake foreground yielding a 5-tuple with `audio_pcm_bytes` on a speak chunk + a NEW fake audio_output stub with `async def push_chunk` (records calls) → assert `push_chunk` awaited with those bytes; an `is_listen=True` (barge-in) chunk → `request_stop`, no push. (b) backward-compat — a 4-tuple fake still drives `run()` (no push, no error). (c) `AudioOutputController.push_chunk` awaits its sink and drops when `_stop_event` is set. NOTE: only the NEW PR6 fake gains `push_chunk`; the existing PR3b/PR3c/etc. fakes are NOT modified (defensive unpack + structural Protocol keep them valid).

## Tests expected to pass UNCHANGED
All existing `test_continuous_*` (4-tuple fakes via defensive unpack); `test_continuous_pr3b_barge_in.py` (the `_AlwaysEnded` mechanism still exists for non-native mode — native just skips it). Confirm green post-build.

## Risks / open
| Item | Handling |
|---|---|
| Gate-relax skip breaks the continuous loop | Probes v2–v4 ran 30+ chunks across turns without the relax (listen→speak→listen) — no block. Verify on redeploy. |
| Low volume (silent ref) | hardcoded ×4 gain in `_gpu_work` for v1; voiced reference = follow-up. |
| chunk_ms=200 audio unproven | v1 pins chunk_ms=1000; 200 = follow-up probe. |
| token2wav latency at 1000ms | measure on redeploy (folds into the clean p95 re-measure). |
| sink is async | resolved — `push_chunk` is `async def` + on `_AudioOutputProtocol`; orchestrator `await`s it. |
| merged-stack barge-in audit (gate-relax → no speech at generate_audio=False) | flagged for a separate audit; out of scope here. |

## Out of scope
Reference-voice cloning; prosody; chunk_ms=200 audio; the merged-stack gate-relax/barge-in audit; the clean p95 re-measure.
