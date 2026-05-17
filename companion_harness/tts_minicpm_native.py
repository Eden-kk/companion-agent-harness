"""MiniCPMNativeTtsAdapter — streaming TTS via per-chunk MiniCPMODuplex.

Uses a second MiniCPMODuplex (generate_audio=True) sharing the foreground
model's weights. Per-chunk PCM via audio_tokenizer.stream() primitive.

See docs/plan-native-tts-streaming-refactor-v2.md for design rationale.

Fixes applied (atomic — see plan §1-2):
  B1: TTS duplex constructed after foreground, before traffic (call ordering).
  B2: asyncio.Lock sufficient — foreground duplex has generate_audio=False and
      never touches audio_tokenizer.stream_cache.
  B3: current_turn_ended = False after prepare() activates listen→tts_bos remap.
  B4: server wiring passes foreground model; no second MiniCPMStreamingModel load.
  CN5: run_coroutine_threadsafe(...).result() for backpressure on queue.put.
  CN6: asyncio.Lock serialises synthesize(); drain+join on CancelledError.
  CN7: _MiniCPMODuplexNoPad removes upstream 1-second left-pad (modeling_minicpmo.py:3586-3590).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import AsyncIterator

import numpy as np

from companion_harness.tts_minicpm_native_compat import _patch_torchaudio

__all__ = ["MiniCPMNativeTtsAdapter"]

_SAMPLE_RATE = 24000


# ---------------------------------------------------------------------------
# CN7-FIX: subclass-via-instance-method-override to remove 1-second left-pad
# ---------------------------------------------------------------------------

class _MiniCPMODuplexNoPad:
    """Installs a patched _generate_waveform_from_tokens on a MiniCPMODuplex."""

    @staticmethod
    def install(base_model):
        from types import MethodType
        # B1-A: second init_tts inside as_duplex(generate_audio=True) replaces
        # model.tts.audio_tokenizer (:266). Foreground duplex with
        # generate_audio=False early-returns before touching token2wav path
        # (modeling_minicpmo.py:3298), so there is no stale-reference issue.
        duplex = base_model.as_duplex(generate_audio=True)
        duplex._generate_waveform_from_tokens = MethodType(
            _generate_waveform_from_tokens_nopad, duplex
        )
        return duplex


def _generate_waveform_from_tokens_nopad(self, new_tokens, prompt_wav_path,
                                          is_last_chunk=False, force_flush=False):
    """Copy of MiniCPMODuplex._generate_waveform_from_tokens
    (HF cache modeling_minicpmo.py:3524-3592, commit
    6e885630cbe907859c441ff915aa789729f3a5c4) with the 1-second left-pad
    at lines 3586-3590 removed.

    Source hash pinned by test_minicpm_native_tts_first_chunk_audible.py.
    """
    if not self.token2wav_initialized:
        return None

    CHUNK_SIZE = 25
    token_ids = list(new_tokens.reshape(-1).tolist())
    self.token2wav_buffer += token_ids
    has_chunk_eos = any(t in self.chunk_terminator_token_ids for t in token_ids)
    pcm_bytes_list = []

    if has_chunk_eos or force_flush:
        while len(self.token2wav_buffer) >= self.pre_lookahead + 5:
            chunk_to_process = min(CHUNK_SIZE + self.pre_lookahead, len(self.token2wav_buffer))
            pcm = self.model.tts.audio_tokenizer.stream(
                self.token2wav_buffer[:chunk_to_process], prompt_wav=prompt_wav_path
            )
            pcm_bytes_list.append(pcm)
            self.token2wav_buffer = self.token2wav_buffer[
                min(CHUNK_SIZE, chunk_to_process - self.pre_lookahead):
            ]
    else:
        while len(self.token2wav_buffer) >= CHUNK_SIZE + self.pre_lookahead:
            pcm = self.model.tts.audio_tokenizer.stream(
                self.token2wav_buffer[:CHUNK_SIZE + self.pre_lookahead],
                prompt_wav=prompt_wav_path,
            )
            pcm_bytes_list.append(pcm)
            self.token2wav_buffer = self.token2wav_buffer[CHUNK_SIZE:]

    if is_last_chunk and len(self.token2wav_buffer) > 0:
        pcm = self.model.tts.audio_tokenizer.stream(
            self.token2wav_buffer, prompt_wav=prompt_wav_path, last_chunk=True
        )
        pcm_bytes_list.append(pcm)
        self.token2wav_buffer = []

    if not pcm_bytes_list:
        return None
    all_pcm = b"".join(pcm_bytes_list)
    if not all_pcm:
        return None
    pcm_np = np.frombuffer(all_pcm, dtype="<i2")
    audio_waveform = pcm_np.astype(np.float32) / 32768.0
    # CN7-FIX: upstream lines 3586-3590 (1-second left-pad) intentionally omitted.
    return audio_waveform


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class MiniCPMNativeTtsAdapter:
    """Streaming TTS adapter backed by a dedicated MiniCPMODuplex(generate_audio=True).

    Shares the foreground model's weights. Constructed AFTER the foreground
    duplex so init_tts completes before any traffic (B1-A).

    Output: PCM16 little-endian at 24 kHz mono (same as KokoroTtsAdapter).
    """

    def __init__(self, streaming_model, *, silent_ref_path=None):
        _patch_torchaudio()
        base = streaming_model._duplex.model
        self._tokenizer = streaming_model._duplex.tokenizer
        self._duplex_tts = _MiniCPMODuplexNoPad.install(base)
        self._silent_ref_path = silent_ref_path or self._write_silent_ref()
        self._synthesize_lock = asyncio.Lock()  # CN6-FIX

    @staticmethod
    def _write_silent_ref():
        import soundfile as sf
        silence = np.zeros(16000, dtype=np.float32)
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="minicpm_silent_ref_")
        os.close(fd)
        sf.write(path, silence, 16000)
        # Tempfile intentionally not deleted: Token2wav reads it per-session;
        # OS cleans /tmp on reboot. One ~32 KB WAV per adapter instance.
        return path

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        # prosody_tags accepted but ignored — MiniCPM-o native TTS has no
        # prosody-tag rendering path.
        async for chunk in self._synthesize_inner(text):
            yield chunk

    async def _synthesize_inner(self, text: str) -> AsyncIterator[bytes]:
        # CN6-FIX: serialize per-instance synthesis. Lock held across the entire
        # generator lifetime to prevent prepare() resetting state while a prior
        # producer is still running.
        async with self._synthesize_lock:
            loop = asyncio.get_running_loop()
            duplex = self._duplex_tts
            ref_path = self._silent_ref_path

            def _prepare_sync():
                duplex.prepare(
                    prefix_system_prompt="Streaming TTS.", prompt_wav_path=ref_path
                )
                duplex.streaming_prefill(text_list=[text])
                duplex.current_turn_ended = False  # B3-FIX: activates listen→tts_bos remap

            await loop.run_in_executor(None, _prepare_sync)
            queue: asyncio.Queue = asyncio.Queue(maxsize=8)

            def _producer_sync():
                try:
                    while True:
                        result = duplex.streaming_generate(listen_prob_scale=0.0)
                        wav = result.get("audio_waveform")
                        if wav is not None and len(wav) > 0:
                            pcm16 = (np.clip(wav, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
                            # CN5-FIX: blocking put — producer threads blocks on full
                            # queue (correct backpressure; put_nowait would drop silently).
                            asyncio.run_coroutine_threadsafe(queue.put(pcm16), loop).result()
                        if result.get("end_of_turn") or result.get("is_listen"):
                            break
                        if duplex.is_break_set():
                            break
                finally:
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

            producer_task = loop.run_in_executor(None, _producer_sync)
            try:
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    yield chunk
            except asyncio.CancelledError:
                duplex.set_break_event()
                # CN6-FIX: drain BEFORE awaiting producer_task to unblock any
                # pending run_coroutine_threadsafe(queue.put).result() in the
                # producer thread. Producer's is_break_set() check on next
                # iteration exits the loop; finally puts sentinel None.
                while True:
                    try:
                        item = queue.get_nowait()
                        if item is None:
                            break
                    except asyncio.QueueEmpty:
                        break
                raise
            finally:
                # Drain residual items so producer can put sentinel and exit.
                while not queue.empty():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await producer_task

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE
