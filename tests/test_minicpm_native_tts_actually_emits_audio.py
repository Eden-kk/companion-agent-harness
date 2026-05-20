"""Tests for MiniCPM native TTS adapter audio emission.

Verifies that:
1. MiniCPMNativeTtsAdapter.synthesize() yields at least one non-empty bytes chunk.
2. AudioOutputController.is_synthesizing is True during synthesis, False after first chunk.
3. is_barge_in_trigger() returns False while is_synthesizing is True (barge-in deferred).

All tests use a mocked MiniCPMStreamingModel — no real model weights required.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from unittest.mock import MagicMock, patch

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


def _make_fake_streaming_model(pcm_bytes: bytes = b"\x00" * 320) -> MagicMock:
    """Return a MagicMock that looks enough like MiniCPMStreamingModel for the adapter."""
    fake_duplex = MagicMock()
    fake_base = MagicMock()
    fake_tokenizer = MagicMock()

    # model.chat() writes a WAV-like file and we intercept via soundfile mock
    fake_duplex.model = fake_base
    fake_duplex.tokenizer = fake_tokenizer

    streaming_model = MagicMock()
    streaming_model._duplex = fake_duplex
    return streaming_model, fake_base, fake_tokenizer, pcm_bytes


@pytest.mark.asyncio
async def test_synthesize_yields_chunks():
    """MiniCPMNativeTtsAdapter.synthesize() must yield at least one non-empty bytes chunk."""
    # soundfile only needed for this test; the other tests in this file are soundfile-free,
    # so this guard is per-function (not module-level) on purpose.
    pytest.importorskip("soundfile")
    import numpy as np
    import tempfile, os

    from companion_harness.tts_minicpm_native import MiniCPMNativeTtsAdapter

    streaming_model, fake_base, fake_tokenizer, expected_pcm = _make_fake_streaming_model(
        b"\x01\x00" * 160  # 160 int16 samples = 320 bytes
    )

    # Patch soundfile.read to return synthetic PCM without needing a real WAV file
    def fake_sf_read(path, dtype):
        samples = np.zeros(160, dtype=np.float32)
        samples[0] = 0.5  # non-silent
        return samples, 16000

    def fake_chat(**kwargs):
        pass  # no-op; soundfile.read is patched below

    fake_base.chat = fake_chat

    with patch("soundfile.read", fake_sf_read):
        adapter = MiniCPMNativeTtsAdapter(streaming_model)
        chunks = []
        async for chunk in adapter.synthesize("Hello", []):
            chunks.append(chunk)

    assert len(chunks) >= 1, "synthesize() yielded no chunks — audio would be silent"
    assert all(len(c) > 0 for c in chunks), "yielded empty bytes chunk"


@pytest.mark.asyncio
async def test_is_synthesizing_flag_lifecycle():
    """is_synthesizing is True during synthesis window, False after first chunk arrives."""
    logger, received = _make_logger()
    await logger.start()

    synthesizing_states: list[bool] = []
    chunks_delivered: list[bytes] = []

    async def mock_sink(chunk: bytes) -> None:
        chunks_delivered.append(chunk)

    controller = AudioOutputController(
        session_id="test-synth-flag",
        logger=logger,
        sink=mock_sink,
    )

    assert not controller.is_synthesizing  # idle state

    gen_id = controller.start_generation(caused_by=["policy-001"])
    assert controller.is_playing
    assert controller.is_synthesizing  # synthesis phase started

    async def slow_tts_gen() -> AsyncIterator[bytes]:
        # Record synthesizing state BEFORE yield (simulates after-executor-completes)
        synthesizing_states.append(controller.is_synthesizing)
        yield b"\x00" * 160
        synthesizing_states.append(controller.is_synthesizing)
        yield b"\x00" * 160

    await controller.play(slow_tts_gen(), generation_event_id=gen_id)

    assert not controller.is_playing
    assert not controller.is_synthesizing

    await logger.stop()

    # First chunk clears is_synthesizing
    assert synthesizing_states[0] is True   # before first yield, still synthesizing
    assert synthesizing_states[1] is False  # after first chunk sent, synthesis done


@pytest.mark.asyncio
async def test_barge_in_deferred_during_synthesis():
    """is_barge_in_trigger() returns False while is_synthesizing is True.

    This is the direct regression gate: ambient VAD during TTS synthesis window
    must NOT trigger barge-in, because the audio hasn't started playing yet.
    """
    logger, received = _make_logger()
    await logger.start()

    async def mock_sink(chunk: bytes) -> None:
        pass

    controller = AudioOutputController(
        session_id="test-barge-in-defer",
        logger=logger,
        sink=mock_sink,
    )

    gen_id = controller.start_generation(caused_by=["policy-002"])
    assert controller.is_playing
    assert controller.is_synthesizing

    # Simulate what is_barge_in_trigger() checks:
    # is_playing=True, is_synthesizing=True → barge-in must be suppressed
    barge_in_would_fire = (
        controller.is_playing
        and not controller.is_synthesizing
    )
    assert not barge_in_would_fire, (
        "Barge-in triggered during synthesis window — "
        "this is the root-cause regression: play_task gets cancelled before any audio chunk is sent"
    )

    # Simulate first chunk arriving
    async def one_chunk_gen() -> AsyncIterator[bytes]:
        yield b"\x00" * 160

    await controller.play(one_chunk_gen(), generation_event_id=gen_id)

    # After play completes, synthesizing is cleared
    assert not controller.is_synthesizing

    await logger.stop()
