"""CPU test: ASRSidecar energy-VAD segmentation + asr_transcript_emitted emit.

No torch / whisper / GPU. Uses a fake ASR model (callable returning fixed text).
"""

from __future__ import annotations

import asyncio

import pytest

from companion_harness.asr_sidecar import ASRSidecar, _RMS_THRESHOLD, _SILENCE_CLOSE_MS, _SAMPLE_RATE
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

_BYTES_PER_SAMPLE = 2
_FRAME_MS = 20  # 20 ms frames
_FRAME_SAMPLES = int(_SAMPLE_RATE * _FRAME_MS / 1000)
_FRAME_BYTES = _FRAME_SAMPLES * _BYTES_PER_SAMPLE


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


def _speech_frame() -> bytes:
    """PCM16 frame whose RMS is well above threshold."""
    amplitude = int(32767 * 0.5)  # 50% full scale >> _RMS_THRESHOLD
    import array
    samples = array.array("h", [amplitude if i % 2 == 0 else -amplitude for i in range(_FRAME_SAMPLES)])
    return samples.tobytes()


def _silence_frame() -> bytes:
    return b"\x00" * _FRAME_BYTES


def _silence_frames_needed() -> int:
    """Number of 20 ms silence frames to trigger utterance close."""
    return (_SILENCE_CLOSE_MS // _FRAME_MS) + 2


async def _run_sidecar(
    asr_model,
    frames: list[tuple[bytes, str]],
    config_store=None,
) -> list[Event]:
    logger, received = _make_logger()
    await logger.start()
    queue: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=512)
    sidecar = ASRSidecar(
        session_id="test-session",
        asr_model=asr_model,
        logger=logger,
        config_store=config_store,
        queue=queue,
    )
    task = asyncio.create_task(sidecar.run())
    for frame, evt_id in frames:
        await queue.put((frame, evt_id))
    # sentinel
    await queue.put((b"", ""))
    await task
    await logger.stop()
    return [e for e in received if e.event_type == "asr_transcript_emitted"]


@pytest.mark.asyncio
async def test_asr_sidecar_emits_on_silence_boundary() -> None:
    """Speech frames followed by enough silence → exactly one asr_transcript_emitted."""
    transcript = "hello world"
    call_count = 0

    def fake_asr(pcm16: bytes, sample_rate: int) -> str:
        nonlocal call_count
        call_count += 1
        return transcript

    n_speech = 20
    n_silence = _silence_frames_needed()
    frames = (
        [(_speech_frame(), f"audio-{i}") for i in range(n_speech)]
        + [(_silence_frame(), f"sil-{i}") for i in range(n_silence)]
    )
    events = await _run_sidecar(fake_asr, frames)

    assert len(events) == 1, f"expected 1 emit, got {len(events)}"
    evt = events[0]
    assert evt.payload_inline["text_preview"] == transcript[:200]
    assert evt.subject_class == "self"
    assert evt.caused_by == [f"audio-{n_speech - 1}"]


@pytest.mark.asyncio
async def test_asr_sidecar_skips_empty_transcript() -> None:
    """Fake ASR returning empty string → no emit."""
    def fake_asr(pcm16: bytes, sample_rate: int) -> str:
        return ""

    n_speech = 10
    n_silence = _silence_frames_needed()
    frames = (
        [(_speech_frame(), f"audio-{i}") for i in range(n_speech)]
        + [(_silence_frame(), f"sil-{i}") for i in range(n_silence)]
    )
    events = await _run_sidecar(fake_asr, frames)
    assert events == [], f"expected no emit on empty transcript, got {len(events)}"


@pytest.mark.asyncio
async def test_asr_sidecar_seam_off_no_emit() -> None:
    """config_store with asr seam OFF → no emit."""
    class _FakeConfigStore:
        def get_seam(self, name: str) -> bool:
            return False  # all seams off

    transcript = "should not appear"

    def fake_asr(pcm16: bytes, sample_rate: int) -> str:
        return transcript

    n_speech = 20
    n_silence = _silence_frames_needed()
    frames = (
        [(_speech_frame(), f"audio-{i}") for i in range(n_speech)]
        + [(_silence_frame(), f"sil-{i}") for i in range(n_silence)]
    )
    events = await _run_sidecar(fake_asr, frames, config_store=_FakeConfigStore())
    assert events == [], f"expected no emit when asr seam is OFF, got {len(events)}"


@pytest.mark.asyncio
async def test_asr_sidecar_multiple_utterances() -> None:
    """Two separate speech bursts separated by silence → two emits."""
    utterances: list[str] = []

    def fake_asr(pcm16: bytes, sample_rate: int) -> str:
        idx = len(utterances)
        utterances.append(f"utterance-{idx}")
        return utterances[-1]

    n_speech = 15
    n_silence = _silence_frames_needed()
    burst = (
        [(_speech_frame(), f"s{i}") for i in range(n_speech)]
        + [(_silence_frame(), f"q{i}") for i in range(n_silence)]
    )
    frames = burst + burst
    events = await _run_sidecar(fake_asr, frames)

    assert len(events) == 2, f"expected 2 emits, got {len(events)}"
    for evt in events:
        assert evt.subject_class == "self"
        assert evt.caused_by
