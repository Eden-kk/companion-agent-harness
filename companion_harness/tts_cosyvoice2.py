"""CosyVoice2TtsAdapter — TtsAdapter backed by CosyVoice2-0.5B.

Implements the TtsAdapter Protocol (tts_adapter.py) using CosyVoice2's
streaming inference API. Produces 24 kHz mono PCM16 LE — same wire format
as KokoroTtsAdapter and MiniCPMNativeTtsAdapter.

## Dependency isolation

``cosyvoice`` is NOT installed in the canonical venv (pip cosyvoice==0.0.8
pulls torch==2.12.0+cu130 and numpy==2.4.5, both incompatible with the
canonical torch==2.11.0+cu128 / numpy<2 pinned by minicpmo-utils).  The
import is therefore LAZY (inside __init__) so this module is importable on
machines without cosyvoice installed.  On b200, install cosyvoice from the
FunAudioLLM git clone pinned to a known-good commit, keeping numpy<2:

    pip install git+https://github.com/FunAudioLLM/CosyVoice@<commit>
    pip install "numpy<2"  # re-pin after cosyvoice install

See requirements-b200.txt for the pinned commit and post-install notes.

## Invariant boundaries

Synthesis is invoked only downstream of a policy-approved SpeakDecision
(invariants #2, #4).  No path to produce audio independently.

## Locking model (§2.4.1)

outer methods (synthesize, synthesize_streaming) acquire _synthesize_lock
exactly ONCE per call.  Inner helpers MUST NOT re-acquire — asyncio.Lock is
non-reentrant; inner re-acquisition deadlocks.  _inference_executor (single
worker) serialises GPU access.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor

import numpy as np

__all__ = ["CosyVoice2TtsAdapter"]

_SAMPLE_RATE = 24000

# 200 ms of PCM16 at 24 kHz mono: 24000 * 0.2 * 2
_MAX_CHUNK_BYTES = 9600

# Flush clause buffer when time elapsed since last drain exceeds this.
_CLAUSE_BUFFER_CAP_MS = 200

_CJK_CLAUSE_ENDS = frozenset("。！？；")
_ASCII_CLAUSE_ENDS = frozenset(".!?;")
_CLAUSE_BOUNDARY_CHARS = _CJK_CLAUSE_ENDS | _ASCII_CLAUSE_ENDS


class CosyVoice2TtsAdapter:
    """TtsAdapter backed by CosyVoice2-0.5B.

    Default mode: inference_sft with built-in '中文女' speaker (no reference
    WAV required).  Zero-shot voice cloning activates when reference_wav is
    provided (requires reference_text too).

    prosody_tags: accepted and ignored (Kokoro parity; future PR translates
    to CosyVoice2 inline SSML).
    """

    def __init__(
        self,
        model_dir: str,
        reference_wav: str | None = None,
        reference_text: str | None = None,
        language: str = "zh",
        speed: float = 1.0,
        warmup: bool = True,
    ) -> None:
        from cosyvoice.cli.cosyvoice import CosyVoice2  # lazy import
        self._cosy = CosyVoice2(model_dir, load_jit=False, fp16=True)
        self._reference_wav = reference_wav
        self._reference_text = reference_text
        self._language = language
        self._speed = speed
        self._synthesize_lock = asyncio.Lock()
        self._inference_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cosyvoice2-",
        )
        if warmup:
            self._warmup_sync()

    def _warmup_sync(self) -> None:
        list(self._run_inference_sync("你好，今天天气不错"))

    def _run_inference_sync(self, text: str):
        """Run CosyVoice2 inference synchronously; yields numpy arrays."""
        if self._reference_wav is not None:
            import torchaudio  # noqa: WPS433
            waveform, sr = torchaudio.load(self._reference_wav)
            ref_audio = waveform[0].numpy()
            for result in self._cosy.inference_zero_shot(
                text,
                self._reference_text,
                ref_audio,
                stream=True,
                speed=self._speed,
            ):
                yield result["tts_speech"]
        else:
            for result in self._cosy.inference_sft(
                text,
                "中文女",
                stream=True,
                speed=self._speed,
            ):
                yield result["tts_speech"]

    def _pcm_from_array(self, arr) -> bytes:
        """Convert float32 numpy array to int16 LE PCM bytes."""
        if hasattr(arr, "numpy"):
            arr = arr.numpy()
        flat = arr.reshape(-1).astype(np.float32)
        return (np.clip(flat, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()

    def _yield_pcm_chunks(self, raw: bytes):
        for offset in range(0, len(raw), _MAX_CHUNK_BYTES):
            yield raw[offset : offset + _MAX_CHUNK_BYTES]

    async def synthesize(
        self, text: str, prosody_tags: list[str]
    ) -> AsyncIterator[bytes]:
        """Stream PCM16 bytes for text. prosody_tags accepted but ignored."""
        async with self._synthesize_lock:
            async for chunk in self._synthesize_locked(text):
                yield chunk

    async def _synthesize_locked(self, text: str) -> AsyncIterator[bytes]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=8)
        cancel_evt = threading.Event()

        def _producer():
            try:
                for arr in self._run_inference_sync(text):
                    if cancel_evt.is_set():
                        break
                    raw = self._pcm_from_array(arr)
                    for chunk in self._yield_pcm_chunks(raw):
                        if cancel_evt.is_set():
                            break
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
            finally:
                asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

        producer_task = loop.run_in_executor(self._inference_executor, _producer)
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield chunk
        except asyncio.CancelledError:
            cancel_evt.set()
            while True:
                try:
                    item = queue.get_nowait()
                    if item is None:
                        break
                except asyncio.QueueEmpty:
                    break
            raise
        finally:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await producer_task

    async def synthesize_streaming(
        self,
        text_chunks: AsyncIterator[str],
        prosody_tags: list[str],
    ) -> AsyncIterator[bytes]:
        """Stream PCM as text chunks arrive. Clause-boundary + wall-cap flush policy.

        Accumulates text into a clause buffer and fires synthesis on:
          (a) a CJK clause-end char (。！？；) or ASCII .!?;
          (b) _CLAUSE_BUFFER_CAP_MS wall-time since last drain
          (c) end-of-stream

        prosody_tags accepted but ignored.
        """
        async with self._synthesize_lock:
            buf: list[str] = []
            last_drain_ms: float = time.monotonic() * 1000

            async def _drain_clause(clause_text: str) -> AsyncIterator[bytes]:
                loop = asyncio.get_running_loop()
                queue: asyncio.Queue = asyncio.Queue(maxsize=8)
                cancel_evt = threading.Event()

                def _producer():
                    try:
                        for arr in self._run_inference_sync(clause_text):
                            if cancel_evt.is_set():
                                break
                            raw = self._pcm_from_array(arr)
                            for chunk in self._yield_pcm_chunks(raw):
                                if cancel_evt.is_set():
                                    break
                                asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                    finally:
                        asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

                producer_task = loop.run_in_executor(self._inference_executor, _producer)
                try:
                    while True:
                        chunk = await queue.get()
                        if chunk is None:
                            break
                        yield chunk
                except asyncio.CancelledError:
                    cancel_evt.set()
                    while True:
                        try:
                            item = queue.get_nowait()
                            if item is None:
                                break
                        except asyncio.QueueEmpty:
                            break
                    raise
                finally:
                    while not queue.empty():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    await producer_task

            async for chunk in text_chunks:
                if not chunk:
                    continue
                buf.append(chunk)
                now_ms = time.monotonic() * 1000
                has_boundary = any(ch in _CLAUSE_BOUNDARY_CHARS for ch in chunk)
                cap_elapsed = (now_ms - last_drain_ms) >= _CLAUSE_BUFFER_CAP_MS
                if has_boundary or cap_elapsed:
                    clause = "".join(buf)
                    buf.clear()
                    last_drain_ms = time.monotonic() * 1000
                    async for pcm in _drain_clause(clause):
                        yield pcm

            if buf:
                clause = "".join(buf)
                async for pcm in _drain_clause(clause):
                    yield pcm

    @property
    def sample_rate(self) -> int:
        return _SAMPLE_RATE
