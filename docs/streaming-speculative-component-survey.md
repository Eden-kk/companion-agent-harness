# Survey: streaming-speculative opportunities across the complete audio path

**Target audience:** v0.3+ planners deciding which sub-systems to make streaming-speculative AFTER Path B (foreground proposer) lands.
**Scope:** complete inventory of basic-stack audio-path components, with current state, streaming opportunity, speculative opportunity, cost, and SOTA reference for each.
**Companion to:** `docs/plan-streaming-speculative-bargein-execution.md` (Path B — foreground proposer only; do not re-read that spec's §3.3–3.5 content here).
**Drafted:** 2026-05-16 at HEAD `62a5196`.

---

## §1 — Mental model

**Streaming** means the component begins emitting useful output before it has consumed its full input. For encoders (ASR, VAD) this means yielding partial transcripts or frame-level signals frame-by-frame rather than accumulating the entire utterance first. For decoders (TTS, LLM token generation) it means the first token or audio chunk leaves the component as soon as it is produced, so the downstream consumer can begin work in parallel. For classifiers (addressing, deictic) it means scoring a partial hypothesis rather than waiting for a complete sentence. For sinks (AudioOutBroker, EventLogger) it means not buffering — forwarding each unit as it arrives, which they already do.

**Speculative** adds a second dimension on top: the component begins computing during a window when its output might be discarded — most commonly during the user's speech interval. The payoff is that when the EOU signal arrives, the expensive inference is already partially or fully done rather than starting from cold. Speculative work is only safe when three conditions hold: the computation is independent of the final input (or sufficiently robust to partial input), cancellation is cheap (the discarded work leaves no state corruption), and the expected completion rate justifies the resource cost. The combination of streaming + speculative gives the maximum latency reduction: the component warms up speculatively, then streams its output incrementally the moment the commit decision is made — so neither the startup cost nor the output-delivery cost sits on the critical path.

---

## §2 — Component-by-component survey

### §2.1 — Audio ingest (`/ws/ingest` → InputIngest)

- **File:** `companion_harness/input_ingest.py:131` (`ingest_chunk`)
- **Current state:** Accepts raw PCM16/16 kHz frames over WebSocket, writes blobs to disk, emits `raw_audio_chunk` events with causal chain (`caused_by=[prev_chunk_id]` or `[session_open_id]` for the first chunk, lines 136–139). Each call returns immediately; the blob write is synchronous but sub-millisecond for 32 ms frames (~1 KB). Session lifecycle is managed by `IngestSession` (line 39): `_seq` counter, previous-chunk ID tracking, and independent causal chains per modality (audio vs video). The `_write_blob` call (line 77) writes to `blob_dir/<event_id>`; the blob URI (`blob://<event_id>`) goes into `payload_ref` so the audio byte content is separated from the event metadata and subject to the configured retention policy.
- **Streaming opportunity:** Already fully streaming — one chunk in, one event out, no batching. The WebSocket layer is aiohttp; chunk size is caller-controlled (browser sends whatever the Web Audio API captures, typically 128–512 samples at 16 kHz = 8–32 ms). No gap to close here: the chunk boundary IS the streaming unit.
- **Speculative opportunity:** None. Ingest is the source node of the DAG; it has no upstream to speculate about.
- **Cost:** N/A
- **SOTA reference:** Standard WebSocket PCM streaming (browser Web Audio API `AudioWorkletProcessor`). The blob-per-event pattern mirrors Kafka log-segment storage: content separated from the index.
- **Status:** OUT-OF-SCOPE (already optimal for its role)

---

### §2.2 — VAD (Silero ONNX)

- **File:** `companion_harness/vad_silero.py:82` (`__call__`)
- **Current state:** Silero VAD v6 ONNX running on CPU. Called once per 32 ms frame (`_WINDOW_SAMPLES = 512`, line 33). Returns `p_speech ∈ [0, 1]`. Stateful — the ONNX model carries its own recurrent GRU state through `_STATE_SHAPE = (2, 1, 128)` (line 37), updated in-place on each forward pass (lines 98–101). Inference cost is ~2 ms/frame on CPU (per `basic-stack-design-2026-05-16.md` hop table). Emits `vad_frame` per call and `vad_turn_signal` when the orchestrator's silence accumulator exceeds the EOU threshold. State is reset between sessions via `reset_states()` (line 78). The model runs on `CPUExecutionProvider` (line 73) — deliberately kept off GPU because at 32 ms/call, 31 calls/s the CPU cost is trivial and avoids GPU context switching.
- **Streaming opportunity:** Already fully streaming — scored per frame, no buffering. The GRU hidden state carried across calls is the correct stateful architecture for real-time speech detection. The only possible streaming improvement would be moving to a sub-frame (8 ms) window for lower detection latency, but Silero v6 is trained at 512-sample windows; a different model would be required.
- **Speculative opportunity:** The VAD silence-threshold accumulator in `_detector_fanout_task` (orchestrator) could be adapted to emit an *early* `vad_turn_signal` with lower confidence when `p_speech` drops sharply (single-frame p_speech transition from >0.8 to <0.3), allowing T2 to start ASR before the full silence-onset timer expires. This "early-fire" speculative EOU would be committed if the next 50–100 ms remains silent, retracted if `p_speech` recovers (speech continuation). Risk: false early-fires on filled pauses ("uh…", "um…") that exhibit the same sharp p_speech transition. A two-frame confirmation window (64 ms) reduces false fires significantly.
- **Cost:** Low code complexity (add a `_speculative_eou` branch in the orchestrator's `_detector_fanout_task`). No GPU cost (VAD runs on CPU). Replay impact: the `vad_turn_signal` event is the Tier-B replay anchor; speculative early fires add events but do not replace existing ones. The retraction event must be logged for DAG closure.
- **SOTA reference:** WebRTC VAD (Google, LGPL) uses similar three-state aggressiveness with a similar early-trigger option (`mode=3`); Silero v5/v6 is the SOTA open-source VAD at this frame duration [5].
- **Status:** PROPOSED (low priority; Path B's native-duplex EOU (`_last_is_listen`) is a higher-ROI EOU improvement that reduces the same latency without the disfluency risk)

---

### §2.3 — SmartTurn (Pipecat v3)

- **File:** `companion_harness/turn_detector_smart.py:63` (`SmartTurnDetector`)
- **Current state:** Disabled in the basic stack (`smart_turn: NO` per `basic-stack-design-2026-05-16.md`). The adapter is complete: accumulates turn audio in `_audio_buffer`, fires the injected `SmartTurnModel` only at silence-candidate moments (after `silence_onset_ms = 300 ms` of continuous silence, line 145). Returns `TurnSignal` with `p_done`, `p_continue`, `p_backchannel=0.0`. The real Pipecat Smart Turn v3 ONNX (~8 MB, ~12 ms CPU inference) is the planned injection.
- **Streaming opportunity:** The current design accumulates the full turn buffer before scoring. Pipecat Smart Turn v3 is a fixed-context model; it scores a fixed-length audio window, not a streaming sequence. True streaming would require a different model architecture (e.g., a causal RNN or the native-duplex `_last_is_listen` signal). The speculative approach (below) is more tractable.
- **Speculative opportunity:** Score the accumulated buffer speculatively at silence-candidate moments that precede the VAD timer expiry — i.e., when `p_speech` drops below threshold but before the hard 300 ms window closes. If `p_done` is high in the pre-timeout window, emit the `TurnSignal` early and let T2 begin ASR. This lookahead halves the expected wait from `silence_onset_ms` to `silence_onset_ms / 2` on average for clean utterance endings.
- **Cost:** Adds one extra Pipecat model invocation per speculative candidate. At ~12 ms CPU per call and typical silence-onset intervals of 300–600 ms, this is a ~4% CPU uplift on T1. Code complexity: medium (requires exposing a second fire-point in `SmartTurnDetector`).
- **SOTA reference:** Pipecat Smart Turn v3 (Daily.co, 2024) — the upstream ONNX model this adapter wraps [6].
- **Status:** PROPOSED (deferred until SmartTurn is enabled; depends on Path B landing first per §4)

---

### §2.4 — Backchannel detector (whisper-tiny + lexicon)

- **File:** `companion_harness/backchannel_asr_lexicon.py:1` (`ASRLexiconBackchannelModel`), `companion_harness/backchannel_classifier.py:1` (`BackchannelClassifier`)
- **Current state:** Disabled in the basic stack. The adapter runs whisper-tiny every N frames (default N=32, ~1 second) on a trailing audio window, scores 1.0 if the transcript fuzzy-matches a backchannel phrase list (`BACKCHANNEL_PHRASES`), else 0.0. Between ASR runs the previous score is reused. `BackchannelClassifier.process_frame` is invoked on every frame so the N-frame cadence is internal.
- **Streaming opportunity:** The current whisper-tiny invocation is batch-mode (full window). Replacing with a streaming VAD+lexicon approach — detect a short voiced segment (<200 ms), then ASR that burst only — would reduce the latency from ~1 s to ~200 ms. Alternatively, running whisper-tiny in streaming mode via `WhisperStreamingTranscriber` [5] on the short backchannel window achieves similar latency.
- **Speculative opportunity:** Because backchannels occur during agent speech, the decision window is inherently low-urgency (the agent is already speaking). Speculative pre-scoring during expected response points (after agent pauses) has low value. The bottleneck is more detection sensitivity than latency.
- **Cost:** Low — whisper-tiny is already loaded for ASR; sharing the model instance is the correct approach rather than a second load.
- **SOTA reference:** Hara et al. "Real-time Backchannel Prediction" (Interspeech 2018); Silero VAD + whisper-tiny burst-ASR is the practical SOTA for open-source deployments.
- **Status:** PROPOSED (low priority; blocked on enabling the backchannel seam first)

---

### §2.5 — ASR (faster-whisper)

- **File:** `companion_harness/asr_faster_whisper.py:47` (`__call__`)
- **Current state:** `FasterWhisperASRModel` wraps faster-whisper whisper-tiny.en (`MODEL_ID = "tiny.en"`, line 40). Called **once per EOU** on the full turn audio buffer accumulated since the previous EOU. Deterministic settings: `temperature=0.0`, `beam_size=1`, `condition_on_previous_text=False`, `vad_filter=False`, `without_timestamps=True` (lines 53–59). The PCM16 buffer is converted to float32 normalized in `__call__` (line 51) and passed as a numpy array to `WhisperModel.transcribe()`. The generator is eagerly joined with `"".join(seg.text for seg in segments).strip()` (line 61) — no streaming consumer. Observed latency 200–500 ms for a typical 3–8 second utterance (per hop table in `basic-stack-design-2026-05-16.md`). Called synchronously in T2 before addressing, blocking the policy gate for the full transcription duration.
- **Streaming opportunity:** faster-whisper's `transcribe()` returns a lazy generator over `Segment` objects; the current code immediately exhausts it with `"".join(...)`. Removing the eager join and instead iterating the generator as segments arrive would let T2 receive the first segment (typically 0.5–1 s of speech) within 80–150 ms of EOU rather than after the full utterance is decoded. Each segment can be emitted as an `asr_partial_transcript` event and forwarded immediately to the addressing classifier. The whisper_streaming [2] local-agreement algorithm adds word-level confirmation: only words confirmed by two consecutive decodings are emitted, preventing hallucinated early words from misleading the classifier. Implementing this requires: (a) keeping the audio buffer alive during streaming rather than passing a frozen snapshot, (b) adding `asr_partial_transcript` to the event schema, (c) handling retraction if a segment is revised.
- **Speculative opportunity:** Begin ASR during user speech using a rolling buffer rather than a post-EOU snapshot. The `_turn_audio_buffer` accumulated in the orchestrator during the user's utterance is available in real time; a concurrent ASR task could process it in 1-second rolling windows while the user is still speaking, using the whisper-streaming local-agreement pattern to lock in confirmed words. At EOU, the speculative transcript is finalized (either accepted as-is or re-run on the final buffer if the delta is large). This approach eliminates ASR latency from the post-EOU critical path entirely on typical utterances (>2 s). The risk is GPU contention: whisper-tiny and MiniCPM-o share the GPU, and continuous whisper-tiny transcription during MiniCPM-o's speculative generation (Path B) could cause queuing. Mitigation: CUDA stream priority or time-sliced scheduling.
- **Cost:** Medium code complexity (T2 must manage the partial-transcript lifecycle, emit retraction events on revision, and coordinate with addressing). GPU: whisper-tiny at CUDA float16 uses ~150 MB VRAM; running it concurrently with MiniCPM-o (Path B) adds contention risk on an 80 GB H100 but is manageable given the size difference. Replay impact: adds `asr_partial_transcript` events; the final `asr_transcript_emitted` event (Tier-B replay anchor) is unchanged.
- **SOTA reference:** Macháček et al. "Turning Whisper into Real-Time Transcription System" (arxiv 2307.14743) — whisper_streaming local-agreement [2]; SYSTRAN/faster-whisper streaming segment generator [5].
- **Status:** PROPOSED (researcher's top post-Path-B opportunity; §4 phase 2)

---

### §2.6 — Addressing classifier (MiniCPM logprob, PR #322)

- **File:** `companion_harness/addressing_classifier.py:150` (`MiniCPMAddressingClassifierImpl.__call__`)
- **Current state:** `MiniCPMAddressingClassifierImpl` constructs a prompt from the full transcript (`_ADDRESSING_PROMPT`, line 139) and calls `classify_yes_no` (`foreground_model_minicpm.py:179`). `classify_yes_no` runs a single forward pass through the MiniCPM-o base model, extracts next-token logits, and computes `p_yes / (p_yes + p_no)` over pre-cached yes/no token IDs (lines 205–217). No autoregressive generation occurs; the KV cache and `_duplex` state are not advanced. Deterministic given fixed weights. Observed latency <100 ms (per hop table). The low-confidence ambiguity band `[0.45, 0.55]` (line 146) triggers an audit event but does not change the binary output. Falls back to `WakeWordAddressingClassifier` (string match on transcript) when `MiniCPMAddressingClassifierImpl` raises.
- **Streaming opportunity:** The current classifier requires a complete transcript string. Since `classify_yes_no` works on any text string, it can be called on a partial transcript from §2.5 with no API change. The only caller-side change is feeding partial segments to the classifier as they arrive rather than waiting for the full `asr_transcript_emitted` event. For most addressed utterances the addressing intent is detectable from the first 2–5 words (e.g., "Can you—", "Hey, could you—", "What do you think—"); `p_yes` on partial text covering these words is typically above 0.7, well clear of the ambiguity band.
- **Speculative opportunity:** Run the classifier on the first confirmed partial-transcript segment speculatively (before EOU), cache the `AddressingSignal`, and compare it to the final-transcript classification at EOU. If both agree (expected >90% of the time for addressed vs non-addressed turns), skip the re-run and use the cached signal. This constitutes the "speculative-permit" path: `SpeakPolicy.decide()` is pre-staged during the user's speech. At EOU, T2 either commits the permit (ASR + addressing already done, policy already staged → immediate T4 dispatch) or retracts and re-runs (<1 ms policy re-run). The net effect is moving ASR + addressing latency entirely off the post-EOU critical path on the happy path.
- **Cost:** One additional `classify_yes_no` forward pass per speculative trigger (every 2 s of user speech, or per partial-segment boundary). Forward pass is <100 ms; marginal GPU cost is real but bounded. Code complexity: medium — T2 must track the speculative permit's transcript fingerprint, confirm or retract at EOU, and emit `addressing_speculative` + `addressing_classified` events in the right order for DAG closure. Replay: `addressing_classified` (Tier-B anchor) is unchanged; speculative events are advisory.
- **SOTA reference:** Kyutai Moshi [3] performs joint speech-text intent detection in one unified model — the extreme end of this approach where the boundary between ASR and addressing classification disappears. The MiniCPM-o logprob path is a lighter-weight approximation that reuses an already-loaded model.
- **Status:** PROPOSED (part of the "streaming ASR + speculative addressing" phase 2; §4)

---

### §2.7 — Deictic detector (MiniCPM, PR #307)

- **File:** `companion_harness/deictic_detector_minicpm.py:34` (`MiniCPMDeicticDetector.__call__`)
- **Current state:** Uses `MiniCPMDuplexModel.chat()` (text-only, line 53) with a yes/no+confidence prompt on the final transcript. Returns `(bool, float)`. Called after ASR, synchronously, inside T2. Graceful failure (malformed response, exception, empty transcript) returns `(False, 0.0)`. Audio frame bytes are accepted for Protocol conformance but unused.
- **Streaming opportunity:** The same partial-transcript streaming opportunity as §2.6 applies. However, deictic detection is inherently weaker on partial utterances than addressing detection: deictic expressions ("look at this", "that one") tend to appear at the end of utterances, not the beginning. Streaming deictic is therefore lower value than streaming addressing.
- **Speculative opportunity:** Negligible unless vision pipeline is active (deictic detection is only actionable when a scene can be queried). With vision off (basic stack), deictic results do not change the policy outcome. Hold for when vision is enabled.
- **Cost:** Low marginal cost given the model is already loaded; the speculative opportunity is low enough that this does not belong in v0.3 planning.
- **SOTA reference:** Same as §2.6 (MiniCPM-o text classifier reuse).
- **Status:** OUT-OF-SCOPE for streaming-speculative planning (vision dependency; low EOU latency impact)

---

### §2.8 — SpeakPolicy

- **File:** `companion_harness/speak_policy.py:33` (`decide`)
- **Current state:** Pure function — no model calls, no state, no I/O. Takes `PolicyInputs`, `signal_event_ids`, and optional `p_backchannel` / `proposal` / thresholds. Returns `SpeakDecision` with `action_type` and `primary_reason_code` from `ReasonCode` enum. Determinism is a hard invariant (#5): the docstring at line 53 enumerates the guarantees — no wall-clock reads, no randomness, no dict-iteration variance, only keyed lookups and boolean tests on `PolicyInputs` fields. Rules evaluate in priority order: hard blocks (social mode, line 68) → alert threshold (line 74) → EOU gate (line 88 approximately) → backchannel (line ~100) → proactivity budget → `SILENCE_WINS_TIES` fallthrough. Execution time <1 ms.
- **Streaming opportunity:** Not applicable. The function is sub-millisecond pure Python; it is not a latency bottleneck.
- **Speculative opportunity:** Because `decide()` is a pure function, it is trivially safe to call speculatively on a partial addressing signal before EOU. If the speculative `AddressingSignal` (from §2.6) indicates `full_response`, a pre-staged `SpeakDecision` is cached. At EOU: if addressing agrees, the cached decision is promoted (zero re-evaluation cost); if addressing disagrees, re-run costs <1 ms. The benefit accrues entirely to the calling orchestrator (T2), which can set `_policy_decision_future.set_result(staged_decision)` immediately at EOU on the fast path, allowing T4 to begin snapshotting the proposal ring without any post-EOU blocking. Crucially, the `policy_decision` event must still be emitted with the final `signal_event_ids` (including the `asr_transcript_emitted` event ID) to satisfy the causal DAG invariant — the staging is an implementation detail invisible to the event log.
- **Cost:** Negligible. Only added complexity is the "staged decision" lifecycle state in T2: a nullable `_staged_decision: SpeakDecision | None` field with a corresponding transcript fingerprint for invalidation. No new dependencies.
- **SOTA reference:** Rules-based policy layers at sub-millisecond cost are universal in spoken dialogue systems. The staging pattern mirrors pre-fetch disambiguation in instruction pipelines (branch prediction).
- **Status:** PROPOSED (automatic corollary of speculative addressing in §2.6; no dedicated PR — implement as part of the phase 2 streaming ASR work)

---

### §2.9 — Foreground proposer (MiniCPM-o)

- **File:** `companion_harness/foreground_model_minicpm.py:226` (`MiniCPMStreamingModel.infer_stream`)
- **Current state:** Turn-batched (Path A). `infer_stream` is an `async def` that returns `_gen()`, an `AsyncGenerator[ThinkerProposal, None]` (lines 226–317). The generator is invoked once per turn via T3 (`_foreground_stream_task`, `realtime_orchestrator.py:963`), gated by `_batch_open_event` set in T2 at EOU. Per 1-second audio chunk: `streaming_prefill(audio_waveform=chunk)` accumulates audio into the KV cache; `streaming_generate(...)` produces one forward pass and returns `{"text": ..., "is_listen": bool}`. When `is_listen=False`, a `ThinkerProposal` is yielded (lines 273–284). The `_process_chunk` inner function (line 256) handles the prefill → generate → emit cycle; `_last_is_listen` (line 267) tracks whether the model is in listen vs speak mode. Cold-start latency 200–800 ms for first token (F0c measurements in `manual-test-findings-2026-05-16.md`). Context is folded at first call only (`duplex.prepare(prefix_system_prompt=combined)`, line 252) — per-turn context re-injection is not supported by MiniCPM-o's current `as_duplex` API.
- **Streaming opportunity:** `infer_stream` already returns an `AsyncGenerator` — the streaming token interface exists. The current gap is that T3 tears down the generator and recreates it per turn via `await _batch_open_event`. Path B's primary change is holding the generator alive for the entire session (continuous lifetime), removing the per-turn cold start entirely. `_last_is_listen = True` is the safe listen-mode default (line 139); Path B promotes `_last_is_listen=False` transitions as the primary EOU producer via `MiniCPMNativeDuplexEouSource`.
- **Speculative opportunity:** This IS the primary speculative target — see `docs/plan-streaming-speculative-bargein-execution.md` Path B for full design. Summary: by running `infer_stream` continuously, the model generates tokens speculatively while the user is speaking; the `_proposal_ring` accumulates them; T4 commits or discards the ring at EOU. The result is that the proposer's first-token cost (200–800 ms) is moved entirely off the post-EOU critical path. Cross-reference: §3.3 of the Path B plan for the ring design; §3.5 for coordinated KV-cache reset.
- **Cost:** See Path B plan §3.3 for the full LOC and complexity estimate (~600 LOC core). The KV cache memory implication: the speculative KV cache grows during user speech; a reset at listen→speak transitions (§3.5 of Path B) keeps it bounded. High complexity, the highest latency payoff of all 14 components.
- **SOTA reference:** Kyutai Moshi [3] (full-duplex with a single model and no separate EOU detector); OpenAI Realtime API [6] (GPT-4o native duplex, <300 ms first-token latency); Gloeckle et al. multi-token prediction [7] (complementary: reduces the number of forward passes per token, orthogonal to the continuous-invocation approach here).
- **Status:** IN-FLIGHT — see `docs/plan-streaming-speculative-bargein-execution.md`

---

### §2.10 — Memory retrieval

- **File:** `companion_harness/memory_manager.py:54` (`MemoryManager` Protocol); concrete stores in `companion_harness/episodic_memory_store.py`, `companion_harness/semantic_relational_store.py`
- **Current state:** `MemoryManager` is a Protocol stub (v0.1c Task 6). The `MemoryManagerStub` returns empty lists. `retrieve()` is called in `ForegroundModel` to populate `context_items` before `infer_stream`. In production the concrete stores will be SQLite-backed (`episodic_memory_store.py`, `semantic_relational_store.py`). Embedding is stubbed via `_NullEmbeddingAdapter` (line 47).
- **Streaming opportunity:** Retrieval is a point query at EOU time; the result size is bounded (a few dozen items). No meaningful streaming dimension — the result set is small enough to fetch in one call (<10 ms for SQLite with indexed lookup).
- **Speculative opportunity:** Run the memory retrieval query speculatively during user speech, using the partial transcript as the query key. At EOU, confirm or re-run with the final transcript. This pre-stages the `context_items` tuple so `infer_stream` (Path B) can fold context without paying the retrieval RTT on the critical path. Particularly valuable when the embedding adapter is real (ANN lookup on a large episodic store could take 50–200 ms).
- **Cost:** Low — retrieval is a read-only query. With the current stub, cost is zero. Once the real embedder is wired, speculative retrieval using partial transcripts may return slightly different results than final-transcript retrieval; the false-retrieval rate must be measured. Code complexity: low (prefetch into a cache keyed by speculative transcript hash, invalidated at EOU if transcript changed significantly).
- **SOTA reference:** Retrieval-augmented generation (RAG) pre-fetch patterns (Lewis et al., NeurIPS 2020 RAG paper). Standard industry practice for latency-sensitive memory systems.
- **Status:** PROPOSED (low complexity; deferred until real memory stores are wired — not a v0.3 blocker)

---

### §2.11 — TTS (Kokoro)

- **File:** `companion_harness/tts_kokoro.py:119` (`KokoroTtsAdapter.synthesize`)
- **Current state:** `synthesize()` is an `async def` that returns an async generator over `Kokoro.create_stream()` (lines 131–140). `Kokoro.create_stream()` is a native streaming API from the `kokoro_onnx` library; it runs ONNX inference on phoneme batches internally and yields `(samples: np.ndarray[float32], sample_rate: int)` tuples as each phoneme batch completes. The adapter converts each tuple to int16 PCM bytes (line 138–140) and yields them. The sample rate is fixed at 24 kHz mono (line 76). Warmup is performed at construction time (line 117) to pre-JIT the ONNX kernels. Observed first-chunk latency: 1–3 s in practice, because Kokoro's internal phoneme batching accumulates enough phonemes to fill one batch before emitting anything — for a short utterance (2–5 words) this is the entire utterance in one batch. The `_synthesizing` flag in `AudioOutputController` is True from `start_generation` until the first chunk arrives in `play()` (line 158 of `audio_output_controller.py`), so the barge-in predicate's `not is_synthesizing` guard blocks barge-in for the entire 1–3 s synthesis window.
- **Streaming opportunity:** Sub-chunking the Kokoro output is the immediate fix (Path B §3.1): after the float32→int16 conversion, if `len(pcm16.tobytes()) > _MAX_CHUNK_BYTES` (where `_MAX_CHUNK_BYTES = 9600` bytes = 200 ms at 24 kHz mono int16), slice into ≤9600-byte segments before yielding. The concatenation of all yielded chunks is byte-identical to the un-sliced output (PCM has no inter-sample state). After this fix, `AudioOutputController.play()` flips `_synthesizing = False` after the first ≤200 ms sub-chunk, opening the barge-in window for the remaining playback. Estimated 30 LOC change to `tts_kokoro.py`.
- **Speculative opportunity:** Pre-warm TTS with the first tokens from the proposer ring (Path B §3.2 above). Kokoro's `create_stream()` takes a complete text string; it does not natively support incremental token feeding. Two approaches for streaming text input: (a) run `create_stream()` on an incrementally extended text string, cancelling and restarting from the new text on each token — wasteful but simple; (b) buffer the first N tokens into a "first sentence" heuristic (when a sentence boundary is detected in the ring tokens), call `create_stream()` on that sentence, then call it again on the remainder at commit time. Approach (b) avoids re-synthesis of the early tokens and achieves sub-200 ms first-audio-chunk latency on committed turns. Cancellation on discard uses the existing `cancel_generation` path (`audio_output_controller.py:132`). This opportunity depends on Path B landing first.
- **Cost:** Sub-chunking: ~30 LOC, zero GPU/CPU cost, zero content fidelity risk, no new dependencies. TTS pre-warming (approach b): medium complexity (~100 LOC), requires Path B `_proposal_ring`, and careful cancellation testing. The spec-canonical TTS target is MiniCPM-o native audio output (`init_tts=True`) once the CUDA 12.8/13.0 version constraint documented in `tts_kokoro.py:14–35` clears; at that point Kokoro can be cleanly replaced behind the `TtsAdapter` Protocol.
- **SOTA reference:** CosyVoice2 and VITS2 support true token-by-token streaming synthesis natively. ElevenLabs Turbo and Cartesia Sonic achieve <100 ms first-chunk latency in production. MiniCPM-o native TTS is the spec-canonical target [8].
- **Status:** PROPOSED (sub-chunking is a prerequisite for Path B §3.1; TTS pre-warming is researcher's #2 opportunity — §4 phase 3)

---

### §2.12 — Audio output broker (AudioOutBroker → /ws/audio_out → browser)

- **File:** `manual_test_console/server.py:322` (`AudioOutBroker`), `manual_test_console/server.py:766` (`_handle_audio_out_ws`)
- **Current state:** `AudioOutBroker.publish()` (line 344) takes `(session_id, seq, chunk)`, wraps in a JSON message with base64-encoded PCM bytes, and does `put_nowait` on each subscriber queue (per-subscriber `asyncio.Queue`, depth `_AUDIO_OUT_QUEUE_DEPTH`). Drop-oldest on overflow (lines 357–368). The WebSocket handler drains the queue and sends via `ws.send_json`. The browser decodes base64 and plays via Web Audio API.
- **Streaming opportunity:** Already fully streaming — each chunk is forwarded to all subscribers as it arrives, with no buffering beyond the per-subscriber queue. The queue depth is the only latency contributor (adds ≤1 queue slot of buffering, i.e., <1 chunk, <200 ms with sub-chunking from §2.11).
- **Speculative opportunity:** None — the broker is a passthrough sink. It cannot speculate about what audio will arrive.
- **Cost:** N/A
- **SOTA reference:** Standard WebSocket binary streaming (aiohttp `ws.send_bytes`). Moving from base64-JSON to binary frames would halve the WS payload size; that is a clean-up, not a streaming-speculative opportunity.
- **Status:** OUT-OF-SCOPE (already optimal; binary-frame upgrade is a separate housekeeping item)

---

### §2.13 — Barge-in / TTS cancellation (`_fire_barge_in`)

- **File:** `companion_harness/realtime_orchestrator.py:1295` (`_fire_barge_in`), `companion_harness/realtime_orchestrator.py:1287` (`is_barge_in_trigger`)
- **Current state:** `is_barge_in_trigger` (line 1287) gates on four conditions: `is_playing AND NOT is_synthesizing AND NOT barge_in_in_flight AND p_backchannel < _p_backchannel_thresh`. The `is_synthesizing` flag in `AudioOutputController` is set True in `start_generation()` (line 92 of `audio_output_controller.py`) and cleared to False in `play()` on the first audio chunk (line 158). With the current Kokoro adapter emitting 1–3 s chunks, `is_synthesizing` is True for nearly the entire synthesis+playback window of short utterances, so the predicate's second condition is never satisfied. `_fire_barge_in` (lines 1295–1337) fires in a background task: calls `request_stop()` (which sets `_stop_event` in `AudioOutputController`), then shields the play task with `asyncio.wait_for(..., timeout=_hard_cancel_after_ms / 1000.0)`, and falls back to `cancel_generation()` on timeout. The play task observes `_stop_event` between chunks and returns early (line 160 of `audio_output_controller.py`).
- **Streaming opportunity:** Already frame-by-frame reactive on the input side (VAD onset events drive the trigger). The gap is on the output side: Kokoro's chunk size prevents `is_synthesizing` from falling within a barge-in-usable window. Fix: sub-chunking (§2.11). After sub-chunking, `is_synthesizing` falls within 200 ms of synthesis start, and the barge-in predicate opens for the remaining playback duration. The `_stop_event` mechanism in `AudioOutputController.play()` already handles mid-stream stops correctly — the stop path is sound; it just cannot be triggered because the predicate never opens.
- **Speculative opportunity:** An "optimistic barge-in" variant would pre-fire `request_stop` at VAD speech onset (before the `is_barge_in_trigger` predicate fully confirms), then commit or retract based on whether the predicate agrees. This avoids a 32 ms guard delay but risks false cancellation on backchannels. The existing `p_backchannel` threshold is the primary protection; an additional 64 ms two-frame-VAD confirmation window before speculative fire keeps false cancellation rates low. In Path B, `_fire_barge_in` must additionally discard the uncommitted `_proposal_ring` tail and call `reset_response_mode()` on the foreground model (Path B §3.4); the speculative pre-fire composes naturally with both extensions.
- **Cost:** Sub-chunking prerequisite: ~30 LOC (§2.11). Optimistic pre-fire: ~20 LOC; risk of false cancellation on backchannels is real but manageable with the two-frame guard. Full Path B barge-in integration (ring discard + model reset): ~250 LOC (see Path B plan §3.4). No new external dependencies.
- **SOTA reference:** Kyutai Moshi [3] uses full-duplex architecture where barge-in is implicit — the model continuously generates in listen/speak mode and speech onset naturally suppresses the speak output. LiveKit Agents targets <500 ms barge-in latency from speech onset; Pipecat implements a similar predicate-gated stop.
- **Status:** IN-FLIGHT (sub-chunking prerequisite is Path B §3.1; full barge-in integration is Path B §3.4; optimistic pre-fire is a follow-on once the predicate is reliably openable)

---

### §2.14 — EventLogger

- **File:** `companion_harness/event_logger.py:59` (`EventLogger.log`)
- **Current state:** Async, non-blocking ring buffer. `log()` (line 59) does `queue.put_nowait(event)` and returns immediately; if the queue is full it increments `_dropped` and returns (lines 62–64) — the realtime path never blocks (invariant #10). The internal `asyncio.Queue` is configured at construction with `maxsize` (class default 1024; server wires 16384 via PR #305, `server.py:1334`). A background `_drain` task (line 80) waits on the queue, calls the primary sink and all subscribers per event (lines 84–89), and emits a `log_drop_or_degrade` event after each batch if `_dropped > 0` (lines 92–97). Subscribers registered via `subscribe()` must precede `start()`; late-subscribers can use `late_subscribe()` which does a list append under the asyncio event loop's single-thread guarantee (line 53). The `caused_by=["_dropped_before_enqueue"]` sentinel in `_make_degrade_event` (line 111) is a known audit gap: `"_dropped_before_enqueue"` is a string literal, not a real event_id, so dropped-event audit chains break. Open as G1/R2-3 in `basic-stack-design-2026-05-16.md §4`.
- **Streaming opportunity:** Already fully streaming — events are forwarded one-by-one as they arrive from the queue. No batching. Subscriber fan-out is also per-event (lines 85–89). The only buffering is the queue itself, which is necessary to decouple the realtime path from sink I/O latency.
- **Speculative opportunity:** None — the logger is a pure sink at the bottom of the DAG. It has no upstream to speculate about and no output to pre-compute.
- **Cost:** N/A. The open correctness issue (G1/R2-3: `_dropped_before_enqueue` sentinel) should be fixed by threading the most-recent upstream event_id through `_drop_oldest_put` in the orchestrator so the degrade event carries a real `caused_by`. This is a correctness fix, not a streaming-speculative change.
- **SOTA reference:** Apache Kafka log-append / ring-buffer design (the harness pattern is a single-node analog: the `asyncio.Queue` is the ring, the drain task is the consumer, and `log_drop_or_degrade` is the producer-backpressure signal). OpenTelemetry batch-span-exporter for comparison on the backpressure-count-and-flush pattern.
- **Status:** OUT-OF-SCOPE for streaming-speculative planning (already streaming; the open G1/R2-3 correctness issue is tracked in `basic-stack-design-2026-05-16.md` and is separate from latency optimization)

---

## §3 — Cross-component dependencies

Some streaming-speculative opportunities only unlock when multiple components change together.

### §3.1 — Streaming ASR + speculative addressing → "speculative-permit" path

When §2.5 (streaming ASR) emits `asr_partial_transcript` events as faster-whisper decodes each segment, §2.6 (addressing classifier) can be called on each partial result. If the logprob classifier returns `is_yes=True` with high confidence on the partial transcript, `SpeakPolicy.decide()` can be staged immediately (§2.8). The staged `SpeakDecision(action_type="full_response")` is held as a speculative permit. At EOU, if the final transcript's addressing result agrees, the permit is committed and TTS begins with zero additional addressing latency. If the result disagrees (rare — addressing intent is usually clear from the first few words), the permit is discarded and policy re-runs (<1 ms). This path eliminates 200–500 ms (ASR) + <100 ms (addressing) from the post-EOU critical path on addressed turns.

Requires: faster-whisper streaming adapter in T2, partial-transcript `caused_by` chain, speculative-permit lifecycle management in `_policy_gate_task`. Does not require Path B (works with turn-batched T3 too, though Path B benefits more because T4 has no grace window to fill).

### §3.2 — Streaming proposer + streaming TTS → pre-warm TTS on first tokens

When Path B's `_proposal_ring` accumulates tokens during user speech, the first token can be forwarded to `KokoroTtsAdapter.synthesize()` as soon as it arrives. Kokoro's `create_stream` API accepts text; if the adapter is modified to accept an async token feed rather than a complete string, TTS synthesis begins during the user's speech (on speculative tokens). At EOU, if the policy commits, the first audio chunk may already be enqueued before the policy decision is logged. If the policy discards, the in-progress Kokoro synthesis is cancelled (same `cancel_generation` path as barge-in). This pre-warming path reduces TTS first-chunk latency from 1–3 s to ~200 ms (Kokoro startup only, not full synthesis).

Requires: Path B (ring buffer), sub-chunked Kokoro (§3.1 of Path B plan / §2.11 here), a streaming text feed from ring-snapshot to Kokoro. The Kokoro ONNX session is not designed for mid-synthesis cancellation at arbitrary token boundaries — cancellation must be at a phoneme-batch boundary, which aligns naturally with the sub-chunking boundary.

### §3.3 — Continuous batching + multi-session → unblock concurrency

Currently the MiniCPM-o model is a singleton (one inference session at a time). Path B's continuous infer_stream further couples the model to a single session lifetime. In a multi-user deployment, vLLM-style continuous batching [4] would allow multiple sessions to share the KV cache prefix and be scheduled round-robin across the model's forward-pass budget. This is explicitly out of scope for v0.3 (single-session basic stack) but is the architectural prerequisite for any multi-user variant.

---

## §4 — Suggested phasing

The ordering below is largest-latency-win first, with dependency ordering as a secondary criterion.

### Phase 1 — Path B foreground (IN-FLIGHT)

The streaming-speculative foreground proposer is already planned in `docs/plan-streaming-speculative-bargein-execution.md`. It is the single highest-ROI change: eliminates the 200–800 ms per-turn cold start, closes the barge-in gap (via Kokoro sub-chunking §3.1 of that plan), and establishes the `_proposal_ring` infrastructure that phases 2–3 build on. Landing Path B first makes every subsequent opportunity cheaper to implement.

### Phase 2 — Streaming ASR + speculative addressing

After Path B lands, the next highest-ROI opportunity is the speculative-permit path (§3.1 above). The post-EOU critical path with Path B is: VAD silence-onset (~200 ms) + ASR (~200–500 ms) + addressing (<100 ms) + policy (<1 ms) + snapshot ring (0 ms, tokens already buffered). ASR is now the dominant term. Streaming ASR with speculative addressing moves ASR and addressing off the post-EOU path entirely on addressed turns. Expected p50 EOU→first-TTS-chunk improvement: 200–400 ms. Implementation surface: T2 `_policy_gate_task` + faster-whisper streaming wrapper + partial-transcript `asr_partial_transcript` event type.

### Phase 3 — TTS pre-warming via proposer first tokens

Once the proposal ring is live (Path B) and Kokoro is sub-chunked (Path B §3.1), wiring the first ring tokens to Kokoro as a streaming text feed eliminates TTS initialization latency from the critical path. Expected improvement: first-audio-chunk latency from ~200–500 ms (current Kokoro cold) to <50 ms (Kokoro already mid-synthesis at EOU commit). Implementation surface: `tts_kokoro.py` streaming text API + T4 ring-to-TTS wiring. This is the most mechanically novel change (Kokoro's ONNX model was not designed for speculative-then-commit synthesis); cancellation semantics must be validated carefully.

### Phase 4 — SmartTurn lookahead / continuous EOU speculation

Enabling SmartTurn (§2.3) with the speculative early-fire mechanism reduces EOU detection latency by ~150 ms on average (half of `silence_onset_ms = 300 ms`). Lower ROI than phases 2–3 because VAD-based EOU is already fast and Path B's native-duplex EOU (`_last_is_listen`) is a better primary detector than SmartTurn anyway. SmartTurn early-fire is valuable primarily as a safety-net EOU when the native-duplex signal is late or stuck. Implement after phase 3 when the overall latency budget reveals this as the remaining bottleneck.

---

## §5 — Out of scope for this survey

- **Cross-modal speculative (vision-driven anticipation of speech):** Vision pipeline is not in the basic stack (`vision: NO`). This survey covers audio path only.
- **Multi-user scheduling / continuous batching at the serving layer:** Single-session architecture assumption; vLLM-style concurrency is a separate design document.
- **On-device vs server-edge inference placement:** b200 is the single target; edge quantization (MiniCPM-o Q4 / Kokoro Q8) is a separate cost-reduction track.
- **Audio codec streaming (Opus, etc.):** Handled by the WebSocket layer; not in scope for model-level streaming.
- **Memory store indexing and ANN retrieval optimization:** The memory retrieval speculative opportunity (§2.10) is noted but depends on the real memory stores being wired first.

---

## §6 — References

1. Leviathan, Y., Kalman, M., & Matias, Y. (2023). Fast Inference from Transformers via Speculative Decoding. arxiv 2211.17192.
2. Macháček, D., Dabre, R., & Bojar, O. (2023). Turning Whisper into Real-Time Transcription System (whisper_streaming, local-agreement algorithm). arxiv 2307.14743.
3. Défossez, A., et al. Moshi: a speech-text foundation model for real-time dialogue. Kyutai tech report, kyutai.org/moshi (2024).
4. Kwon, W., et al. Efficient Memory Management for Large Language Model Serving with PagedAttention. SOSP 2023. arxiv 2309.06180.
5. SYSTRAN. faster-whisper: faster implementation of Whisper using CTranslate2. GitHub: SYSTRAN/faster-whisper. Streaming API: `WhisperModel.transcribe()` returns a generator over `Segment` objects.
6. OpenAI. Realtime API documentation. platform.openai.com/docs/guides/realtime (GPT-4o native duplex, <300 ms latency target).
7. Gloeckle, F., et al. Better & Faster Large Language Models via Multi-token Prediction. arxiv 2404.19737.
8. OpenBMB. MiniCPM-o 4.5 technical report and as_duplex API. GitHub: OpenBMB/MiniCPM-o.
