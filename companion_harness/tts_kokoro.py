"""KokoroTtsAdapter — concrete TtsAdapter (live-loop Task 3, Path C).

This module implements the ``TtsAdapter`` Protocol (which IS the spec's
``ProsodyController`` adapter slot — see ``tts_adapter.py`` docstring) using
Kokoro-82M-ONNX. It is the live-loop's first audible synthesis path.

## Why Kokoro (Path C), not MiniCPM-o native (Path A) or CosyVoice2 (Path B)

The milestone draft (``docs/milestone-live-loop-integration-draft.md`` OQ-1)
leans MiniCPM-o native ``as_duplex`` audio as the spec-canonical path, with
CosyVoice2 (spec Part 9) as the named fallback. Both are blocked in the
current canonical b200 venv:

* Path A (MiniCPM-o native ``as_duplex`` audio) requires loading the model
  with ``init_tts=True``, which imports ``stepaudio2`` → ``torchaudio 2.11`` →
  ``libcudart.so.13``. The installed torch is ``2.11.0+cu128`` (CUDA 12.8);
  only ``libcudart.so.12`` is present on the host. Forcing ``libcudart.so.13``
  via ``LD_LIBRARY_PATH`` exposes a second mismatch: torchaudio 2.11 was
  compiled against CUDA 13.0 while torch was compiled against CUDA 12.8, so
  the extension refuses to load. Resolving this requires either a host-level
  CUDA-13 install or a coordinated torch+torchaudio downgrade across the
  whole venv — both invasive to MiniCPM-o which is already working.
* Path B (CosyVoice2) installs cleanly on PyPI but pulls a heavy dependency
  graph (lightning, hydra-core, omegaconf, openai-whisper, diffusers,
  modelscope) into the canonical venv with no model weights pre-staged. The
  install footprint alone risks destabilising the MiniCPM-o text-path that
  v0.1a's latency tests already depend on.

Kokoro-82M is a pure ONNX model (no torch interaction, no libcudart
dependency), ~310 MB on disk, runs on CPU via ``onnxruntime`` and on GPU via
``onnxruntime-gpu`` (already installed). It exposes both a one-shot
``create()`` and a native async ``create_stream()`` that yields per-phoneme
audio chunks — the latter is the natural fit for the ``TtsAdapter`` Protocol's
``AsyncIterator[bytes]`` return type. The ``TtsAdapter`` interface is
unchanged; CosyVoice2 / MiniCPM-o native remain cheap swaps when their
platform blockers clear.

## Tradeoffs (documented per task brief)

* Quality: Kokoro-82M is a small (82M-parameter) TTS. It produces clearly
  intelligible English speech but is not voice-cloned and has no native
  prosody-tag rendering. ``prosody_tags`` are currently accepted and ignored.
  Expressive rendering is deferred to a richer backend (CosyVoice2 or
  MiniCPM-o native) once a platform constraint is resolved.
* Determinism: ONNX inference is reproducible at the model level (same
  weights, same input → same output). The async chunking inside
  ``Kokoro.create_stream`` adds asyncio-level non-determinism in chunk
  *timing* but not in chunk *content*. Per the task brief, synthesis
  stochasticity is acceptable downstream of the deterministic policy layer
  (invariant #5 is a policy-replay invariant, not a synthesis invariant).
* Sample rate: Kokoro emits 24 kHz mono float32. This adapter converts to
  16-bit PCM bytes (little-endian) — the wire format the rest of the harness
  assumes (16 kHz / 16-bit elsewhere; downstream resampling, if any, is the
  playback sink's concern, not the synthesis adapter's).

## Invariant boundaries (non-negotiable)

Synthesis is invoked ONLY downstream of a policy-approved ``SpeakDecision``
(invariants #2, #4). ``KokoroTtsAdapter`` has no path to produce audio
independently — it is a pure function from (text, prosody_tags) to a byte
stream. The orchestrator (``realtime_orchestrator.py`` ``_synthesis_dispatch_task``)
structurally enforces the policy → synthesis edge.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, AsyncIterator

import numpy as np
from kokoro_onnx import Kokoro

__all__ = ["KokoroTtsAdapter"]


# Kokoro emits 24 kHz mono. AudioOutputController is sample-rate-agnostic; the
# playback sink is responsible for any resampling.
_SAMPLE_RATE = 24000

# 200 ms of PCM16 at 24 kHz mono: 24000 samples/s * 0.2 s * 2 bytes/sample
_MAX_CHUNK_BYTES = 9600

# Wall-time cap to flush clause buffer when no boundary character appears.
_CLAUSE_BUFFER_CAP_MS = 200

_CLAUSE_BOUNDARY_CHARS = frozenset(".!?,;")


class KokoroTtsAdapter:
    """``TtsAdapter`` concrete implementation backed by Kokoro-82M-ONNX.

    Model and voices files must be available on disk; see the module docstring
    for the canonical b200 paths. The default voice is ``af_bella``; callers
    can override at construction time.

    Cancellation: ``synthesize()`` returns an async generator. When the
    consumer's task is cancelled, ``CancelledError`` propagates into the
    generator at its ``yield`` point. ``Kokoro.create_stream`` uses an
    internal ``asyncio.Queue`` whose background worker is also cancelled by
    asyncio when the parent task ends; no manual ``aclose()`` is required.
    The protocol contract in ``tts_adapter.py`` covers the rest.
    """

    def __init__(
        self,
        model_path: str,
        voices_path: str,
        voice: str = "af_bella",
        speed: float = 1.0,
        lang: str = "en-us",
        warmup: bool = True,
    ) -> None:
        self._kokoro = Kokoro(model_path, voices_path)
        if voice not in self._kokoro.voices:
            raise ValueError(
                f"voice {voice!r} not in Kokoro voices: "
                f"{sorted(self._kokoro.voices)}"
            )
        self._voice = voice
        self._speed = speed
        self._lang = lang
        if warmup:
            # Run a tiny synthesis once so the onnxruntime kernels are JIT'd
            # before the first real utterance. Without this, first-byte latency
            # is dominated by kernel compile, not synthesis.
            self._kokoro.create("hi", voice=self._voice, speed=1.0, lang=self._lang)

    async def synthesize(
        self, text: str, prosody_tags: list[str]
    ) -> AsyncIterator[bytes]:
        """Stream PCM16 audio bytes for the given text.

        ``prosody_tags`` is accepted for Protocol conformance and ignored — see
        the module docstring's tradeoffs section. A future backend (CosyVoice2
        / MiniCPM-o native) can honour the tags without a Protocol change.

        Each yielded chunk is one Kokoro-internal phoneme-batch worth of
        audio, converted from float32 [-1, 1] to little-endian int16 PCM.
        """
        async for samples, _sr in self._kokoro.create_stream(
            text,
            voice=self._voice,
            speed=self._speed,
            lang=self._lang,
        ):
            # float32 [-1, 1] → int16 little-endian PCM bytes
            clipped = np.clip(samples, -1.0, 1.0)
            pcm16 = (clipped * 32767.0).astype("<i2")
            raw = pcm16.tobytes()
            for offset in range(0, len(raw), _MAX_CHUNK_BYTES):
                yield raw[offset : offset + _MAX_CHUNK_BYTES]

    async def synthesize_streaming(
        self,
        text_chunks: AsyncIterator[str],
        prosody_tags: list[str],
    ) -> AsyncGenerator[bytes, None]:
        """Stream PCM out as text chunks arrive. Hybrid clause-boundary + wall-cap policy.

        Accumulates incoming text into a clause buffer and fires Kokoro's
        create_stream when either (a) a clause-boundary character is seen or
        (b) _CLAUSE_BUFFER_CAP_MS wall-time has elapsed since the last drain.
        Remaining buffer is flushed when text_chunks is exhausted.
        """
        buf: list[str] = []
        last_drain_ms: float = time.monotonic() * 1000

        async def _drain(text: str) -> AsyncIterator[bytes]:
            async for samples, _sr in self._kokoro.create_stream(
                text,
                voice=self._voice,
                speed=self._speed,
                lang=self._lang,
            ):
                clipped = np.clip(samples, -1.0, 1.0)
                pcm16 = (clipped * 32767.0).astype("<i2")
                raw = pcm16.tobytes()
                for offset in range(0, len(raw), _MAX_CHUNK_BYTES):
                    yield raw[offset : offset + _MAX_CHUNK_BYTES]

        async for chunk in text_chunks:
            if not chunk:
                continue
            buf.append(chunk)
            now_ms = time.monotonic() * 1000
            has_boundary = any(ch in _CLAUSE_BOUNDARY_CHARS for ch in chunk)
            cap_elapsed = (now_ms - last_drain_ms) >= _CLAUSE_BUFFER_CAP_MS
            if has_boundary or cap_elapsed:
                text = "".join(buf)
                buf.clear()
                last_drain_ms = time.monotonic() * 1000
                async for pcm in _drain(text):
                    yield pcm

        if buf:
            text = "".join(buf)
            async for pcm in _drain(text):
                yield pcm

    @property
    def sample_rate(self) -> int:
        """Output sample rate in Hz. Kokoro is 24 kHz."""
        return _SAMPLE_RATE
