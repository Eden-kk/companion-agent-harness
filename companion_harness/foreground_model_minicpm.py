"""MiniCPM-o 4.5 concrete implementation of the DuplexModel Protocol.

This module lives on b200 only — it imports torch and transformers.
Import it only when running under the b200 venv (CUDA available).
The ForegroundModel adapter in foreground_model.py stays SDK-free.

Text path (v0.1a latency tests):
    Loads text-only (init_vision=False, init_audio=False, init_tts=False).
    chat(text) → str is the only inference entry point.

Streaming path (Task 2 — as_duplex mode):
    MiniCPMStreamingModel loads with init_audio=True so the audio tower is
    available.  infer_stream() drives MiniCPMODuplex.streaming_prefill() +
    streaming_generate() per 1-second chunk without TTS (generate_audio=False).


Usage:
    # text-only (v0.1a):
    from companion_harness.foreground_model_minicpm import MiniCPMDuplexModel
    model = MiniCPMDuplexModel()
    out = model.chat(question_text)

    # streaming/duplex (Task 2):
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    model = MiniCPMStreamingModel()
    # model satisfies StreamingDuplexModel; inject into ForegroundModel(model=model, ...)
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, AsyncGenerator, AsyncIterator

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

from companion_harness.schemas import Event, MemoryItem, ThinkerProposal

if TYPE_CHECKING:
    from companion_harness.event_logger import EventLogger

__all__ = ["MiniCPMDuplexModel", "MiniCPMStreamingModel"]

_MODEL_ID = "openbmb/MiniCPM-o-4_5"

# 16 kHz PCM16 — 1 second of audio = 16000 int16 samples = 32000 bytes
_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = _SAMPLE_RATE  # 1-second chunks match MiniCPMODuplex default CHUNK_MS=1000


class MiniCPMDuplexModel:
    """DuplexModel backed by MiniCPM-o 4.5 text inference.

    Loads on first instantiation. Callers should reuse a single instance.

    Implements the DuplexModel Protocol: infer(audio_frame) → ThinkerProposal | None.
    Also exposes chat(text) for direct latency measurement without audio framing.
    """

    def __init__(self) -> None:
        self._model = AutoModel.from_pretrained(
            _MODEL_ID,
            trust_remote_code=True,
            attn_implementation="sdpa",
            torch_dtype=torch.bfloat16,
            init_vision=False,
            init_audio=False,
            init_tts=False,
        ).eval().cuda()
        self._tokenizer = AutoTokenizer.from_pretrained(_MODEL_ID, trust_remote_code=True)
        # Warm up CUDA kernels with three calls of varying input length so that
        # the JIT cache covers different token-sequence shapes before measurement.
        for _q in ("Ready?", "Is the sky blue?", "What is the color of the sky today?"):
            self.chat(_q, max_new_tokens=4)

    def chat(self, text: str, max_new_tokens: int = 8) -> str:  # 8 tokens fits direct_question_001 closed yes/no answers; revisit if fixture gains open-ended questions
        """Send a text question and return the model's text response."""
        with torch.no_grad():
            return self._model.chat(
                msgs=[{"role": "user", "content": text}],
                tokenizer=self._tokenizer,
                max_new_tokens=max_new_tokens,
                generate_audio=False,
                enable_thinking=False,
            )

    def infer(self, audio_frame: bytes) -> ThinkerProposal | None:
        """DuplexModel Protocol: not used in text-path latency tests; returns None."""
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        self._memory_context = list(items)


class MiniCPMStreamingModel:
    """StreamingDuplexModel backed by MiniCPM-o 4.5 in as_duplex mode.

    Loads with init_audio=True so the audio tower is available for streaming
    prefill.  TTS is disabled (generate_audio=False) — Task 3 will wire audio
    output via a TtsAdapter.

    Implements both DuplexModel (infer) and StreamingDuplexModel (infer_stream).

    `init_vision` (default False) gates the MiniCPM-o vision tower. When True
    the model is loaded with `init_vision=True` and `streaming_prefill` accepts
    `frame_list=[PIL.Image]` alongside audio so video frames reach the model.
    The default-off path preserves bit-for-bit backward compat with the
    audio-only manual-test pipeline.
    """

    SOURCE = "minicpm_streaming"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        *,
        init_vision: bool = False,
        logger: "EventLogger | None" = None,
        session_id: str = "",
    ) -> None:
        base = AutoModel.from_pretrained(
            _MODEL_ID,
            trust_remote_code=True,
            attn_implementation="sdpa",
            torch_dtype=torch.bfloat16,
            init_vision=init_vision,
            init_audio=True,
            init_tts=True,
        ).eval().cuda()
        self._base = base
        self._tokenizer = AutoTokenizer.from_pretrained(_MODEL_ID, trust_remote_code=True)

        self._duplex = base.as_duplex(generate_audio=False)
        # Tracks is_listen from the most recent streaming_generate call.
        # True (listen) is the safe default — EOU has not fired yet.
        self._last_is_listen: bool = True
        # event_id of the most recent native_duplex_invocation event.
        # None until the first streaming_generate call completes.
        self._last_native_duplex_event_id: str | None = None
        self._logger = logger
        self._session_id = session_id
        self._seq = 0

    def set_session(self, session_id: str, logger: "EventLogger") -> None:
        """Bind this singleton model to a new ingest session.

        Called by ForegroundModel.__init__ when the injected model is a
        MiniCPMStreamingModel so that _emit_invocation logs its events under
        the correct session_id and to the correct per-session EventLogger.
        Overwrites any previous binding — safe because the server allows only
        one active streaming session at a time (the model is a singleton).
        """
        self._session_id = session_id
        self._logger = logger
        # Reset per-session sequence so event IDs do not bleed across sessions.
        self._seq = 0
        self._last_native_duplex_event_id = None
        self._last_is_listen = True

    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        """Send a text question and return the model's text response.

        Reuses the already-loaded base model for text-only inference (no audio
        framing). Mirrors MiniCPMDuplexModel.chat() so MiniCPMDeicticDetector
        can be wired against the streaming model (issue #169).
        """
        with torch.no_grad():
            return self._base.chat(
                msgs=[{"role": "user", "content": text}],
                tokenizer=self._tokenizer,
                max_new_tokens=max_new_tokens,
                generate_audio=False,
                enable_thinking=False,
            )

    def classify_yes_no(self, prompt: str) -> tuple[bool, float]:
        """Logprob-based binary classification.

        Runs ONE forward pass on the prompt and compares the next-token
        logprob mass on yes-tokens vs no-tokens. No autoregressive generation
        happens, so KV cache and state of self._duplex / self._base are not
        advanced. Deterministic given fixed weights.

        Returns:
            (is_yes, prob_yes) where prob_yes is the softmax mass on the
            yes-token set, normalized against the no-token set: p / (p_yes + p_no).
        """
        if not hasattr(self, "_yes_no_token_ids"):
            tok = self._tokenizer
            yes_ids: list[int] = []
            no_ids: list[int] = []
            for word in (" yes", " Yes", " YES", "yes", "Yes"):
                ids = tok.encode(word, add_special_tokens=False)
                if len(ids) == 1:
                    yes_ids.append(ids[0])
            for word in (" no", " No", " NO", "no", "No"):
                ids = tok.encode(word, add_special_tokens=False)
                if len(ids) == 1:
                    no_ids.append(ids[0])
            assert yes_ids and no_ids, "tokenizer produced no single-token yes/no ids"
            self._yes_no_token_ids = (yes_ids, no_ids)

        yes_ids, no_ids = self._yes_no_token_ids
        inputs = self._tokenizer(prompt, return_tensors="pt").to(self._base.device)
        with torch.no_grad():
            # MiniCPMO.forward(data, ...) requires a multimodal data dict (input_ids,
            # position_ids, image_bound, audio_bounds, audio_features, ...). For
            # pure-text logprob classification we bypass the wrapper and call the
            # underlying LLM directly. self._base.llm is a Qwen3ForCausalLM (see
            # modeling_minicpmo.py:117) and is always present regardless of
            # init_vision / init_audio. use_cache=False so we do NOT touch
            # self._base.llm_past_key_values, which MiniCPMODuplex.streaming_generate
            # relies on for the streaming audio session.
            out = self._base.llm(
                input_ids=inputs["input_ids"],
                attention_mask=inputs.get("attention_mask"),
                use_cache=False,
            )
        last_logits = out.logits[0, -1, :]
        probs = torch.softmax(last_logits, dim=-1)
        p_yes = float(sum(probs[i].item() for i in yes_ids))
        p_no  = float(sum(probs[i].item() for i in no_ids))
        if p_yes + p_no <= 0.0:
            return False, 0.5
        prob_yes = p_yes / (p_yes + p_no)
        return prob_yes > 0.5, prob_yes

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        """DuplexModel Protocol stub — single-frame path not used for streaming."""
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        self._memory_context = list(items)

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple[MemoryItem, ...] = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        """Coroutine returning an AsyncGenerator of ThinkerProposal candidates.

        Accumulates PCM16/16kHz audio bytes into 1-second chunks, calls
        streaming_prefill + streaming_generate per chunk, and yields a
        ThinkerProposal whenever the model decides to speak (is_listen=False).
        """
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            duplex = self._duplex
            base_prompt = "Streaming Omni Conversation."
            if context_items:
                # MiniCPM-o does not support mid-session re-prepare; context is
                # folded at first-call only. Follow-up: v0.1j to pass live query.
                numbered = "\n".join(
                    f"{i + 1}. {item.user_visible_summary.value}"
                    for i, item in enumerate(context_items)
                    if item.user_visible_summary is not None
                )
                combined = f"{base_prompt}\nRecent context:\n{numbered}"
            else:
                combined = base_prompt
            duplex.prepare(prefix_system_prompt=combined)

            buf = np.array([], dtype=np.float32)

            def _process_chunk(pcm_float: np.ndarray) -> ThinkerProposal | None:
                duplex.streaming_prefill(audio_waveform=pcm_float)
                result = duplex.streaming_generate(
                    max_new_speak_tokens_per_chunk=duplex.max_new_speak_tokens_per_chunk,
                    temperature=duplex.temperature,
                    top_k=duplex.top_k,
                    top_p=duplex.top_p,
                    listen_prob_scale=duplex.listen_prob_scale,
                    text_repetition_penalty=duplex.text_repetition_penalty,
                    text_repetition_window_size=duplex.text_repetition_window_size,
                )
                self._last_is_listen = bool(result.get("is_listen", True))
                invocation_evt = self._emit_invocation(
                    self._last_is_listen, caused_by
                )
                self._last_native_duplex_event_id = invocation_evt.event_id
                text = result.get("text", "")
                if not self._last_is_listen and text:
                    return ThinkerProposal(
                        proposal_type="observation",
                        content=text,
                        trigger="speech",
                        confidence=0.9,
                        novelty=0.5,
                        interruption_cost=0.3,
                        max_utterance_ms=5000,
                        cooldown_consumed="speech_turn",
                        caused_by=list(caused_by),
                    )
                return None

            async for audio_bytes, video_bytes in frame_iter:
                # If a video frame is paired with this audio chunk, prefill
                # the MiniCPM-o vision tower with it BEFORE the audio chunk
                # is consumed (frame_list goes into the same KV cache).
                # Per b200 pre-verification: streaming_prefill takes
                # frame_list=[PIL.Image], not image= or images=.
                if video_bytes is not None:
                    from PIL import Image  # local import: optional dep
                    import io  # noqa: WPS433
                    img = Image.open(io.BytesIO(video_bytes)).convert("RGB")
                    duplex.streaming_prefill(frame_list=[img])

                # PCM16 → float32 normalised to [-1, 1]
                samples = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
                buf = np.concatenate([buf, samples])
                while len(buf) >= _CHUNK_SAMPLES:
                    chunk, buf = buf[:_CHUNK_SAMPLES], buf[_CHUNK_SAMPLES:]
                    proposal = _process_chunk(chunk)
                    if proposal is not None:
                        yield proposal

            # drain any sub-second tail
            if len(buf) > 0:
                # pad to full chunk with silence so streaming_prefill succeeds
                pad = np.zeros(_CHUNK_SAMPLES - len(buf), dtype=np.float32)
                chunk = np.concatenate([buf, pad])
                proposal = _process_chunk(chunk)
                if proposal is not None:
                    yield proposal

        return _gen()

    async def infer_stream_continuous(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        *,
        on_proposal: Callable[[ThinkerProposal], None],
    ) -> None:
        """Path B: never-terminating consumption + callback per proposal.

        Unlike infer_stream (Path A) which tears down when frame_iter ends,
        this method runs until frame_iter is exhausted or cancelled, invoking
        on_proposal() for each yielded proposal. Used by Path B's continuous
        T3 lifetime so the KV cache is not reset per turn (§3.5 handles reset).
        """
        gen = await self.infer_stream(frame_iter, caused_by)
        async for proposal in gen:
            on_proposal(proposal)

    def reset_streaming_session(self, *, caused_by: list[str]) -> "Event":
        """Clear duplex KV cache (audio_past_key_values + llm_past_key_values).

        Called by orchestrator at turn boundaries to bound first-proposal
        latency. Preserves token2wav cache (TTS speaker state). After this
        call, the next streaming_prefill() must be preceded by
        duplex.prepare(prefix_system_prompt=...), which the next
        infer_stream() invocation handles at line 252-265.
        """
        self._duplex.reset_session(reset_token2wav_cache=False)
        self._last_is_listen = True
        self._last_native_duplex_event_id = None
        return self._emit_session_reset(caused_by)

    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_session_reset(self, caused_by: list[str]) -> "Event":
        seq = self._next_seq()
        now_ms = int(time.monotonic() * 1000)
        event_id = f"{self._session_id}-nd-reset-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"minicpm_session_reset:{event_id}:{now_ms}".encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=self.SCHEMA_VERSION,
            seq_no=seq,
            event_type="minicpm_session_reset",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=self.SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline={
                "reset_at_ms": now_ms,
                "reset_token2wav_cache": False,
                "trigger": "post_turn",
            },
        )
        if self._logger is not None:
            self._logger.log(evt)
        return evt

    def _emit_invocation(self, is_listen: bool, caused_by: list[str]) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-nd-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"native_duplex_invocation:{event_id}:{is_listen}:{now_ms}".encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=self.SCHEMA_VERSION,
            seq_no=seq,
            event_type="native_duplex_invocation",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=self.SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline={"is_listen": is_listen, "ts_mono_ms": now_ms},
        )
        if self._logger is not None:
            self._logger.log(evt)
        return evt
