"""Unit tests for MiniCPMRawStreamingDriver.

Uses a fake foreground model exposing infer_stream() that returns a
pre-canned AsyncGenerator, and a fake TTS adapter that yields a fixed
audio chunk. No real model inference runs in these tests.

API CONSTRAINT NOTE: MiniCPMStreamingModel does not support continuous
audio-in/audio-out via streaming_generate(audio_iter). This driver uses
infer_stream() (the real available API), which yields ThinkerProposal
(text). Proposals bypass SpeakPolicy and are synthesized via tts_adapter.
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


class _FakeForeground:
    """Fake StreamingDuplexModel that yields a fixed set of ThinkerProposals.

    Yields one proposal per audio frame received (up to len(proposals)),
    so tests don't need to exhaust the frame_iter to get proposals back.
    """

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
        remaining = list(self._proposals)

        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                if remaining:
                    yield remaining.pop(0)
        return _gen()


class _FakeForegroundNoProposals(_FakeForeground):
    def __init__(self) -> None:
        super().__init__([])


class _FakeTtsAdapter:
    """TTS adapter that yields a fixed PCM chunk per synthesize() call."""

    def __init__(self, chunk: bytes = b"\x00\x01") -> None:
        self._chunk = chunk
        self.calls: list[str] = []

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        self.calls.append(text)
        yield self._chunk


class _FakeBroker:
    """AudioOutSinkTarget that records published chunks."""

    def __init__(self) -> None:
        self.published: list[tuple[str, int, bytes]] = []

    def publish(self, session_id: str, seq: int, chunk: bytes) -> None:
        self.published.append((session_id, seq, chunk))


class _FakeLogger:
    """Minimal logger that records events."""

    def __init__(self) -> None:
        self.logged: list[Any] = []

    def log(self, event: Any) -> None:
        self.logged.append(event)


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minicpm_raw_streaming_driver_pumps_audio_to_foreground() -> None:
    """Driver calls foreground.infer_stream with audio chunks from push_audio."""
    proposal = _make_proposal("hello world")
    fg = _FakeForeground([proposal])
    tts = _FakeTtsAdapter(b"\xAB\xCD")
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-sess",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-001")
    # Wait past DEBOUNCE_MS so the debounce window fires and TTS completes.
    await asyncio.sleep(_DEBOUNCE_MS / 1000 + 0.1)
    await driver.stop()

    # foreground.infer_stream was called
    assert len(fg.calls) == 1
    # TTS was called with the proposal text (bypassing SpeakPolicy)
    assert "hello world" in tts.calls
    # Audio chunk was published to broker
    assert len(broker.published) == 1
    assert broker.published[0][2] == b"\xAB\xCD"


@pytest.mark.asyncio
async def test_minicpm_raw_streaming_driver_emits_session_event_once() -> None:
    """Driver emits exactly one minicpm_raw_streaming_session_started event per session."""
    fg = _FakeForegroundNoProposals()
    tts = _FakeTtsAdapter()
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-sess-2",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 16, "evt-100")
    driver.push_audio(b"\x00" * 16, "evt-101")
    await asyncio.sleep(0.01)
    await driver.stop()

    session_events = [
        e for e in logger.logged
        if e.event_type == "minicpm_raw_streaming_session_started"
    ]
    assert len(session_events) == 1
    assert session_events[0].payload_inline["warning"] == "policy/audit gates bypassed; demo mode only"


@pytest.mark.asyncio
async def test_minicpm_raw_streaming_driver_no_policy_decisions() -> None:
    """No policy_decision events are emitted by the driver."""
    proposal = _make_proposal("something")
    fg = _FakeForeground([proposal])
    tts = _FakeTtsAdapter()
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-sess-3",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 32, "evt-200")
    await asyncio.sleep(0.01)
    await driver.stop()

    policy_events = [e for e in logger.logged if e.event_type == "policy_decision"]
    assert len(policy_events) == 0


@pytest.mark.asyncio
async def test_minicpm_raw_streaming_driver_session_event_payload_warns_bypass() -> None:
    """The session event payload must include bypassed gate names."""
    fg = _FakeForegroundNoProposals()
    tts = _FakeTtsAdapter()
    broker = _FakeBroker()
    logger = _FakeLogger()

    driver = MiniCPMRawStreamingDriver(
        session_id="test-sess-4",
        foreground_model=fg,
        tts_adapter=tts,
        audio_out_broker=broker,
        logger=logger,
    )

    await driver.start()
    driver.push_audio(b"\x00" * 16, "evt-300")
    await asyncio.sleep(0.01)
    await driver.stop()

    evt = next(e for e in logger.logged if e.event_type == "minicpm_raw_streaming_session_started")
    bypassed = evt.payload_inline.get("bypassed", [])
    assert "SpeakPolicy" in bypassed
    assert "ASR" in bypassed
