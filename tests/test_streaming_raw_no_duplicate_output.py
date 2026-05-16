"""Contract tests: --minicpm-streaming-raw dedup uses debounce-then-synthesize (Option C).

When MiniCPM emits multiple proposals in rapid succession, each new proposal
resets the debounce timer. Only after DEBOUNCE_MS of silence does synthesis
fire — on the latest accumulated content. This prevents the Option A bug
where every mid-thought partial cancelled the in-flight TTS, resulting in
audio only being heard after proposals stopped arriving entirely.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any

import pytest

from companion_harness.minicpm_raw_streaming_driver import MiniCPMRawStreamingDriver, _DEBOUNCE_MS
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
    """Fake foreground that yields N proposals spaced `interval_s` apart on the first frame."""

    def __init__(self, proposals: list[ThinkerProposal], interval_s: float = 0.0) -> None:
        self._proposals = proposals
        self._interval_s = interval_s
        self.calls: list[dict] = []

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        self.calls.append({"caused_by": caused_by})
        proposals = list(self._proposals)
        interval_s = self._interval_s

        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            # Consume one frame to unblock the generator, then yield all proposals.
            async for _ in frame_iter:
                for p in proposals:
                    yield p
                    if interval_s > 0:
                        await asyncio.sleep(interval_s)
                return

        return _gen()


class _RecordingTtsAdapter:
    """TTS adapter that records calls and completions; yields after a short delay."""

    def __init__(self, *, delay: float = 0.01, chunk: bytes = b"\x00\x01") -> None:
        self._delay = delay
        self._chunk = chunk
        self.calls: list[str] = []
        self.completed: list[str] = []

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        self.calls.append(text)
        await asyncio.sleep(self._delay)
        self.completed.append(text)
        yield self._chunk


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
async def test_burst_of_proposals_only_synthesizes_latest_after_debounce() -> None:
    """5 proposals arrive 50ms apart; only ONE TTS call fires (the last content)."""
    # Proposals arrive at 50ms intervals, all within DEBOUNCE_MS of each other.
    proposals = [_make_proposal(f"text-{i}") for i in range(5)]
    fg = _FakeForegroundMultiProposal(proposals, interval_s=0.05)
    tts = _RecordingTtsAdapter(delay=0.01)
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-debounce-burst",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-001")
    # Wait: 5 proposals × 50ms = 250ms emission, plus DEBOUNCE_MS + TTS delay.
    await asyncio.sleep(0.05 * 5 + _DEBOUNCE_MS / 1000 + 0.1)
    await driver.stop()

    # Only ONE TTS call — the last proposal's content.
    assert len(tts.calls) == 1, f"Expected 1 TTS call (debounce), got {len(tts.calls)}: {tts.calls}"
    assert tts.calls[0] == "text-4"
    # That call completed and produced audio.
    assert len(broker.published) >= 1


@pytest.mark.asyncio
async def test_single_proposal_synthesizes_after_debounce() -> None:
    """A single proposal fires TTS exactly once, after DEBOUNCE_MS."""
    fg = _FakeForegroundMultiProposal([_make_proposal("single")])
    tts = _RecordingTtsAdapter(delay=0.01)
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-debounce-single",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-002")
    # Before debounce fires, no TTS yet.
    await asyncio.sleep(_DEBOUNCE_MS / 1000 * 0.5)
    assert len(tts.calls) == 0, "TTS should not fire before debounce window expires"

    # After debounce fires.
    await asyncio.sleep(_DEBOUNCE_MS / 1000 + 0.1)
    await driver.stop()

    assert len(tts.calls) == 1
    assert tts.calls[0] == "single"
    assert len(broker.published) >= 1


@pytest.mark.asyncio
async def test_proposal_after_settle_synthesizes_independently() -> None:
    """Two proposals separated by > DEBOUNCE_MS each trigger their own TTS call."""

    class _TwoWaveForeground:
        """Yields p1, waits > DEBOUNCE_MS, then yields p2."""

        async def infer_stream(
            self,
            frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
            caused_by: list[str],
            context_items: tuple = (),
        ) -> AsyncGenerator[ThinkerProposal, None]:
            async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
                async for _ in frame_iter:
                    yield _make_proposal("wave-1")
                    # Wait well past DEBOUNCE_MS so wave-1 fully settles.
                    await asyncio.sleep(_DEBOUNCE_MS / 1000 + 0.1)
                    yield _make_proposal("wave-2")
                    return

            return _gen()

    fg = _TwoWaveForeground()
    tts = _RecordingTtsAdapter(delay=0.01)
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-debounce-two-wave",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-003")
    # Allow both proposals to emit and both debounce windows to expire.
    await asyncio.sleep(2 * (_DEBOUNCE_MS / 1000 + 0.1) + 0.2)
    await driver.stop()

    # Both proposals are sufficiently separated → 2 independent TTS calls.
    assert len(tts.calls) == 2, f"Expected 2 TTS calls, got {len(tts.calls)}: {tts.calls}"
    assert "wave-1" in tts.calls
    assert "wave-2" in tts.calls


@pytest.mark.asyncio
async def test_no_raw_proposal_superseded_events_during_debounce() -> None:
    """Pure debounce-overruns must NOT emit raw_proposal_superseded_by_newer events."""
    proposals = [_make_proposal(f"q{i}") for i in range(3)]
    fg = _FakeForegroundMultiProposal(proposals)
    tts = _RecordingTtsAdapter(delay=0.01)
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-no-supersede-event",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-010")
    await asyncio.sleep(_DEBOUNCE_MS / 1000 + 0.15)
    await driver.stop()

    superseded_events = [
        e for e in logger.logged
        if e.event_type == "raw_proposal_superseded_by_newer"
    ]
    assert len(superseded_events) == 0, (
        f"Debounce should not emit superseded events, got {len(superseded_events)}"
    )
