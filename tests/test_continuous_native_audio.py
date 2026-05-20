"""PR6 tests: native single-duplex audio routing for ContinuousOrchestrator.

CPU-runnable (no GPU, no real weights). Three assertions:
  (a) Orchestrator routes PCM audio from 5-tuple speak chunk via push_chunk;
      is_listen chunk triggers request_stop and no push_chunk.
  (b) Backward-compat: 4-tuple fake drives run() with no error and no push_chunk.
  (c) AudioOutputController.push_chunk: awaits sink; drops on _stop_event.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


# ---------------------------------------------------------------------------
# (a) Orchestrator routes audio via push_chunk
# ---------------------------------------------------------------------------

class _FiveТupleForeground:
    """Fake foreground: yields one speak 5-tuple then one listen 5-tuple."""

    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple, None]:
        # Speak chunk: is_listen=False, audio_pcm=b"PCMDATA"
        yield (False, "hi", 10, b"PCMDATA", "evt-speak")
        # Listen chunk: is_listen=True, no audio
        yield (True, "", 11, b"", "evt-listen")

    def inject_scratchpad(self, text: str) -> None:
        pass


class _RecordingAudioOutput:
    """Audio output stub that records push_chunk calls and simulates is_playing."""

    def __init__(self) -> None:
        self.push_calls: list[bytes] = []
        self.stop_calls: int = 0
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start_generation(self, caused_by: list[str]) -> str:
        self._playing = True
        return "gen-evt"

    def request_stop(self, caused_by: list[str]) -> str:
        self.stop_calls += 1
        self._playing = False
        return "stop-evt"

    async def push_chunk(self, pcm_bytes: bytes, *, caused_by: list[str]) -> None:
        self.push_calls.append(pcm_bytes)
        self._playing = True  # simulate playback state after first chunk


@pytest.mark.asyncio
async def test_orchestrator_routes_audio_push_chunk() -> None:
    """Speak chunk → push_chunk called; listen chunk while playing → request_stop, no push."""
    logger, _ = _make_logger()
    await logger.start()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    audio_out = _RecordingAudioOutput()

    orch = ContinuousOrchestrator(
        session_id="pr6-test-a",
        logger=logger,
        audio_in=audio_in,
        foreground_model=_FiveТupleForeground(),
        audio_output=audio_out,
    )

    # Sentinel pushed after the fake generator exhausts naturally
    audio_in.put_nowait((b"", ""))
    await orch.run()
    await logger.stop()

    # Speak chunk: push_chunk received the PCM bytes
    assert b"PCMDATA" in audio_out.push_calls, (
        f"expected push_chunk(b'PCMDATA'), got calls: {audio_out.push_calls}"
    )

    # Listen chunk while playing: request_stop called; push_chunk NOT called for the listen chunk
    assert audio_out.stop_calls >= 1, "expected request_stop on listen-while-playing chunk"
    # The listen chunk's audio_pcm=b"" so push_chunk would not have been called even without
    # the is_listen guard — confirm push_calls has exactly the speak chunk bytes
    assert audio_out.push_calls == [b"PCMDATA"], (
        f"expected only speak-chunk push, got: {audio_out.push_calls}"
    )


# ---------------------------------------------------------------------------
# (b) Backward-compat: 4-tuple fake still works
# ---------------------------------------------------------------------------

class _FourТupleForeground:
    """Fake foreground using the existing 4-tuple format (no audio bytes)."""

    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, None, str], None]:
        yield (True, "", None, "evt-old")

    def inject_scratchpad(self, text: str) -> None:
        pass


class _NullAudioOutputBC:
    """Null audio output — never playing, no push_chunk."""

    @property
    def is_playing(self) -> bool:
        return False

    def start_generation(self, caused_by: list[str]) -> str:
        return "noop"

    def request_stop(self, caused_by: list[str]) -> str:
        return "noop"

    async def push_chunk(self, pcm_bytes: bytes, *, caused_by: list[str]) -> None:
        raise AssertionError(f"push_chunk should not be called; got {pcm_bytes!r}")


@pytest.mark.asyncio
async def test_four_tuple_backward_compat() -> None:
    """4-tuple stream_chunks drives run() with no error and no push_chunk call."""
    logger, _ = _make_logger()
    await logger.start()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    audio_out = _NullAudioOutputBC()

    orch = ContinuousOrchestrator(
        session_id="pr6-test-b",
        logger=logger,
        audio_in=audio_in,
        foreground_model=_FourТupleForeground(),
        audio_output=audio_out,
    )

    audio_in.put_nowait((b"", ""))
    await orch.run()  # must not raise
    await logger.stop()


# ---------------------------------------------------------------------------
# (c) AudioOutputController.push_chunk
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_output_controller_push_chunk() -> None:
    """push_chunk awaits sink; drops when _stop_event is set."""
    received: list[bytes] = []

    async def _recording_sink(chunk: bytes) -> None:
        received.append(chunk)

    logger, log_events = _make_logger()
    await logger.start()

    ctrl = AudioOutputController(
        session_id="pr6-test-c",
        logger=logger,
        sink=_recording_sink,
    )

    # Normal push: sink receives bytes
    await ctrl.push_chunk(b"x", caused_by=["evt-c"])
    assert received == [b"x"]

    # After stop_event set: push drops
    ctrl._stop_event.set()
    await ctrl.push_chunk(b"y", caused_by=["evt-c"])
    assert received == [b"x"], f"expected drop after stop_event, got {received}"

    await logger.stop()

    # invariant #1: the pushed chunk was logged (audit event emitted)
    assert any(e.event_type == "assistant_audio_buffer_queued" for e in log_events), (
        f"push_chunk must log an audit event; got {[e.event_type for e in log_events]}"
    )
