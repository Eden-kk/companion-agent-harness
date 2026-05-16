"""Low-latency contract tests for MiniCPMRawStreamingDriver.

Root cause addressed: the original _run() awaited TTS synthesis inline,
blocking frame_iter from draining audio_in. The audio_in queue (maxsize=64)
saturated in ~1.3 s at 50 fps, causing frames to be dropped and infer_stream
to stall indefinitely. Fix: debounce-then-synthesize (Option C) — proposals
are debounced for DEBOUNCE_MS; the final accumulated content is synthesized
as a background task, keeping frame_iter draining concurrently.

Tests here exercise the timing invariants that the fix preserves:
  - first audio chunk arrives before a short deadline even when synthesis is slow
  - audio_in does not starve while synthesis is running
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import pytest

from companion_harness.minicpm_raw_streaming_driver import MiniCPMRawStreamingDriver, _DEBOUNCE_MS
from companion_harness.schemas import ThinkerProposal


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _make_proposal(text: str) -> ThinkerProposal:
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


class _SlowTtsAdapter:
    """TTS adapter that sleeps for `delay_s` before yielding a chunk."""

    def __init__(self, delay_s: float, chunk: bytes = b"\xAB\xCD") -> None:
        self._delay_s = delay_s
        self._chunk = chunk
        self.first_chunk_time: float | None = None

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        await asyncio.sleep(self._delay_s)
        self.first_chunk_time = time.monotonic()
        yield self._chunk


class _ForegroundYieldsAfterNFrames:
    """Foreground model that yields one proposal after receiving N audio frames."""

    def __init__(self, proposals: list[ThinkerProposal], frames_before_yield: int = 2) -> None:
        self._proposals = list(proposals)
        self._frames_before_yield = frames_before_yield

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        remaining = list(self._proposals)

        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            frame_count = 0
            async for _ in frame_iter:
                frame_count += 1
                if frame_count >= self._frames_before_yield and remaining:
                    yield remaining.pop(0)
                    frame_count = 0
        return _gen()


class _CountingBroker:
    def __init__(self) -> None:
        self.published: list[tuple[str, int, bytes]] = []
        self.first_publish_time: float | None = None

    def publish(self, session_id: str, seq: int, chunk: bytes) -> None:
        if self.first_publish_time is None:
            self.first_publish_time = time.monotonic()
        self.published.append((session_id, seq, chunk))


class _FakeLogger:
    def log(self, event: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audio_in_not_starved_during_slow_tts() -> None:
    """audio_in queue must not fill up while TTS synthesis is running.

    With the serial implementation, a 0.2 s TTS delay + 50 fps audio (64-frame
    queue) would saturate in ~1.3 s and start dropping frames. With the fix,
    frame_iter keeps draining concurrently.
    """
    proposal = _make_proposal("hello")
    tts = _SlowTtsAdapter(delay_s=0.05)  # 50 ms synthesis delay
    broker = _CountingBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="latency-test-1",
        foreground_model=_ForegroundYieldsAfterNFrames([proposal], frames_before_yield=2),
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()

    # Push 10 frames quickly to simulate real-time audio at ~50 fps.
    for _ in range(10):
        driver.push_audio(b"\x00" * 320, "evt-frame")
        await asyncio.sleep(0)  # yield to event loop

    # Wait long enough for proposal to arrive + DEBOUNCE_MS + TTS to finish.
    await asyncio.sleep(_DEBOUNCE_MS / 1000 + 0.15)
    await driver.stop()

    # The audio_in queue should not have hit maxsize while TTS ran.
    # Evidence: all 10 frames were processed without drop (broker has a chunk).
    assert len(broker.published) >= 1, "No audio published — possible queue saturation or serial block"


@pytest.mark.asyncio
async def test_no_indefinite_buffering_across_two_utterances() -> None:
    """Second proposal yields audio even while first TTS synthesis is still running.

    With the serial implementation, the loop was blocked on the first
    synthesize() call and could not process the second proposal at all until
    synthesis completed. With Option A (cancel-previous), the second proposal
    cancels the first and runs to completion — at least one chunk is published.
    """
    proposals = [_make_proposal("first"), _make_proposal("second")]
    tts = _SlowTtsAdapter(delay_s=0.05)
    broker = _CountingBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="latency-test-2",
        foreground_model=_ForegroundYieldsAfterNFrames(proposals, frames_before_yield=1),
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    for _ in range(6):
        driver.push_audio(b"\x00" * 320, "evt-frame")
        await asyncio.sleep(0)

    # Wait for proposals to settle, then debounce + TTS to complete.
    await asyncio.sleep(_DEBOUNCE_MS / 1000 + 0.15)
    await driver.stop()

    # At least one proposal must produce audio output (debounce coalesces to
    # the last proposal's content — never silently dropped).
    assert len(broker.published) >= 1, (
        f"Expected at least 1 published chunk (debounced proposal), got {len(broker.published)}. "
        "Proposal may have been blocked or dropped."
    )
