"""Contract tests: --minicpm-streaming-raw dedup uses cancel-previous (Option A).

When MiniCPM emits multiple proposals in rapid succession, each new proposal
cancels the in-flight TTS task (newest wins). Superseded proposals are logged
as raw_proposal_superseded_by_newer for audit (invariant #1).
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


class _FakeForegroundMultiProposal:
    """Fake foreground that yields N proposals back-to-back on the first frame."""

    def __init__(self, proposals: list[ThinkerProposal]) -> None:
        self._proposals = proposals
        self.calls: list[dict] = []

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        self.calls.append({"caused_by": caused_by})
        proposals = list(self._proposals)

        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            # Consume one frame to unblock the generator, then yield all proposals.
            async for _ in frame_iter:
                for p in proposals:
                    yield p
                return

        return _gen()


class _SlowTtsAdapter:
    """TTS adapter that records concurrent call count and yields after a short delay."""

    def __init__(self, *, delay: float = 0.05, chunk: bytes = b"\x00\x01") -> None:
        self._delay = delay
        self._chunk = chunk
        self.calls: list[str] = []
        self.completed: list[str] = []
        self.max_concurrent: int = 0
        self._concurrent: int = 0

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        self.calls.append(text)
        self._concurrent += 1
        if self._concurrent > self.max_concurrent:
            self.max_concurrent = self._concurrent
        try:
            await asyncio.sleep(self._delay)
            self.completed.append(text)
            yield self._chunk
        finally:
            self._concurrent -= 1


class _InstantTtsAdapter:
    """TTS adapter that returns immediately — used for sequential-run tests."""

    def __init__(self, chunk: bytes = b"\x00\x01") -> None:
        self._chunk = chunk
        self.calls: list[str] = []

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        self.calls.append(text)
        yield self._chunk


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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_proposals_latest_wins() -> None:
    """Three proposals arrive back-to-back; only the LAST one's TTS completes."""
    proposals = [_make_proposal(f"text-{i}") for i in range(3)]
    fg = _FakeForegroundMultiProposal(proposals)
    tts = _SlowTtsAdapter(delay=0.05)
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-dedup",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-001")
    # Wait long enough for all proposals to be processed and final TTS to complete.
    await asyncio.sleep(0.3)
    await driver.stop()

    # The last proposal's TTS completes; earlier ones are cancelled mid-synthesis.
    assert "text-2" in tts.completed
    # Peak concurrency never exceeded 1 (cancel-previous serializes synthesis).
    assert tts.max_concurrent <= 1


@pytest.mark.asyncio
async def test_superseded_proposals_emit_audit_event() -> None:
    """Proposals cancelled by a newer proposal emit raw_proposal_superseded_by_newer."""
    proposals = [_make_proposal(f"p{i}") for i in range(3)]
    fg = _FakeForegroundMultiProposal(proposals)
    tts = _SlowTtsAdapter(delay=0.05)
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-supersede-audit",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-010")
    await asyncio.sleep(0.3)
    await driver.stop()

    superseded_events = [
        e for e in logger.logged
        if e.event_type == "raw_proposal_superseded_by_newer"
    ]
    # 3 proposals back-to-back: proposal 0 superseded by 1, proposal 1 superseded by 2.
    assert len(superseded_events) == 2


@pytest.mark.asyncio
async def test_sequential_proposals_after_synthesis_complete_both_run() -> None:
    """After TTS completes, the next proposal is accepted — dedup only fires during synthesis."""
    p1 = _make_proposal("first")
    p2 = _make_proposal("second")

    class _SequentialForeground:
        """Yields p1, waits for TTS to finish, then yields p2."""

        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def infer_stream(
            self,
            frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
            caused_by: list[str],
            context_items: tuple = (),
        ) -> AsyncGenerator[ThinkerProposal, None]:
            self.calls.append({"caused_by": caused_by})

            async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
                async for _ in frame_iter:
                    yield p1
                    # Let TTS complete before emitting second proposal.
                    await asyncio.sleep(0.1)
                    yield p2
                    return

            return _gen()

    fg = _SequentialForeground()
    tts = _InstantTtsAdapter()
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-sequential",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-020")
    await asyncio.sleep(0.3)
    await driver.stop()

    # Both proposals ran — no supersession because first TTS finishes before second arrives.
    assert len(tts.calls) == 2
    assert "first" in tts.calls
    assert "second" in tts.calls
    superseded_events = [
        e for e in logger.logged
        if e.event_type == "raw_proposal_superseded_by_newer"
    ]
    assert len(superseded_events) == 0


@pytest.mark.asyncio
async def test_cancel_path_clears_tts_in_flight_flag() -> None:
    """When a TTS task is cancelled mid-stream, _tts_in_flight is cleared to False."""

    class _BlockingTtsAdapter:
        """TTS that blocks until cancelled."""

        def __init__(self) -> None:
            self.calls: list[str] = []
            self.started = asyncio.Event()

        async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
            self.calls.append(text)
            self.started.set()
            # Block until cancelled.
            await asyncio.sleep(10)
            yield b"\x00"  # never reached

    fg = _FakeForegroundMultiProposal([_make_proposal("blocking")])
    tts = _BlockingTtsAdapter()
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-cancel-flag",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-030")
    # Wait until TTS has started.
    await asyncio.wait_for(tts.started.wait(), timeout=2.0)
    assert driver._tts_in_flight is True

    await driver.stop()
    # After cancellation, flag must be cleared.
    assert driver._tts_in_flight is False
