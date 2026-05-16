"""Contract test: --minicpm-streaming-raw first TTS task must emit audio to broker.

Root-cause regression guard for the silent-audio bug:
  model.chat() silently swallows TTS synthesis errors (bare except: in library),
  leaving the output WAV file empty. sf.read() on the empty file raises
  LibsndfileError, which propagated through run_in_executor into the async
  generator and was swallowed by `except Exception: pass` in
  _synthesize_and_publish — leaving broker.publish never called.

Fix:
  1. tts_minicpm_native: check WAV file size before sf.read; return b"" + log
     error when WAV is empty (chat() failed internally).
  2. minicpm_raw_streaming_driver: replace `except Exception: pass` with
     structured event logging (raw_tts_synthesis_error) + logging.error.

This test uses a fake TTS that yields real bytes to verify the happy path
end-to-end (broker.publish called ≥1 time with non-empty bytes), and a
separate test with a raising TTS to verify the error path emits a
raw_tts_synthesis_error event instead of silently producing nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import pytest

from companion_harness.minicpm_raw_streaming_driver import MiniCPMRawStreamingDriver
from companion_harness.schemas import ThinkerProposal


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_proposal(text: str = "hello") -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="observation",
        content=text,
        trigger="speech",
        confidence=0.9,
        novelty=0.5,
        interruption_cost=0.3,
        max_utterance_ms=5000,
        cooldown_consumed="speech_turn",
        caused_by=[],
    )


class _FakeForeground:
    """Fake StreamingDuplexModel that yields one proposal per audio frame received."""

    def __init__(self, proposals: list[ThinkerProposal]) -> None:
        self._proposals = proposals

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        remaining = list(self._proposals)

        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                if remaining:
                    yield remaining.pop(0)

        return _gen()


class _RaisingTtsAdapter:
    """TTS adapter that raises on synthesize() — simulates model.chat() internal failure."""

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        raise RuntimeError("sf.read failed: LibsndfileError: Format not recognised")
        yield b""  # makes this an async generator (unreachable)


class _EmptyByteTtsAdapter:
    """TTS adapter that yields b'' — simulates empty WAV returned by synthesize()."""

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b""


class _RealByteTtsAdapter:
    """TTS adapter that yields real PCM bytes — happy path."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


class _FakeBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, int, bytes]] = []

    def publish(self, session_id: str, seq: int, chunk: bytes) -> None:
        self.published.append((session_id, seq, chunk))


class _FakeLogger:
    def __init__(self) -> None:
        self.logged: list[Any] = []

    def log(self, event: Any) -> None:
        self.logged.append(event)


# ---------------------------------------------------------------------------
# Happy-path test: broker.publish called with non-empty bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_tts_task_emits_audio_chunks_to_broker() -> None:
    """When TTS yields bytes, broker.publish must be called with non-empty bytes."""
    pcm = b"\x01\x02" * 512  # 1024 bytes of fake PCM
    fg = _FakeForeground([_make_proposal("say something")])
    tts = _RealByteTtsAdapter([pcm])
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="sess-audio-emit",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-001")
    await asyncio.sleep(0.05)
    await driver.stop()

    # At least one non-empty chunk must reach the broker.
    non_empty = [p for p in broker.published if p[2]]
    assert len(non_empty) >= 1, (
        f"Expected broker.publish to be called with non-empty bytes, "
        f"got published={broker.published}"
    )
    assert non_empty[0][2] == pcm


# ---------------------------------------------------------------------------
# Error-path test: raising TTS emits raw_tts_synthesis_error event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tts_exception_emits_error_event_not_silent() -> None:
    """When TTS raises, a raw_tts_synthesis_error event must be logged (not silently swallowed)."""
    fg = _FakeForeground([_make_proposal("trigger error")])
    tts = _RaisingTtsAdapter()
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="sess-tts-error",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-002")
    await asyncio.sleep(0.05)
    await driver.stop()

    error_events = [
        e for e in logger.logged if e.event_type == "raw_tts_synthesis_error"
    ]
    assert len(error_events) >= 1, (
        "Expected raw_tts_synthesis_error event when TTS raises, "
        f"got logged events: {[e.event_type for e in logger.logged]}"
    )
    assert "RuntimeError" in error_events[0].payload_inline["error_summary"]


# ---------------------------------------------------------------------------
# Empty-bytes test: b"" chunk is not published to broker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_tts_chunk_not_published_to_broker() -> None:
    """When synthesize() yields b'', broker.publish must NOT be called (invariant: no empty audio)."""
    fg = _FakeForeground([_make_proposal("empty output")])
    tts = _EmptyByteTtsAdapter()
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="sess-empty-chunk",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-003")
    await asyncio.sleep(0.05)
    await driver.stop()

    assert broker.published == [], (
        f"Expected no publish calls for empty TTS chunk, got {broker.published}"
    )
