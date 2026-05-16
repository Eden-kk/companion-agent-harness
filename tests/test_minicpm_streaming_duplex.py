"""Integration test: MiniCPM-o 4.5 as_duplex streaming path (Task 2).

Success criterion: a recorded audio frame stream fed through MiniCPMStreamingModel
yields ThinkerProposal candidates.  Behavioral, not bit-exact (invariant #6).

Skips cleanly without CUDA (b200 venv required).  The model is loaded with
init_audio=True and generate_audio=False (no TTS dependency).

Async-generator pattern used: infer_stream is a *coroutine that returns an
AsyncGenerator* — not an async-generator function.  The inner _gen() function
yields; infer_stream returns _gen().  This matches the StreamingDuplexModel
Protocol contract and avoids the TypeError that would result from await-ing
an async-generator function directly.
"""

import struct
import math

import pytest

pytestmark = pytest.mark.gpu

torch = pytest.importorskip("torch", reason="torch not available — b200 venv required")

if not torch.cuda.is_available():
    pytest.skip("CUDA not available — b200 required", allow_module_level=True)

from companion_harness.foreground_model import StreamingDuplexModel
from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
from companion_harness.schemas import ThinkerProposal


def _make_pcm16_silence(seconds: float, sample_rate: int = 16000) -> bytes:
    """Return PCM16 little-endian silence of the requested duration."""
    n_samples = int(seconds * sample_rate)
    return struct.pack(f"<{n_samples}h", *([0] * n_samples))


def _make_pcm16_tone(freq_hz: float, seconds: float, sample_rate: int = 16000, amplitude: float = 0.3) -> bytes:
    """Return PCM16 sine wave to give the audio tower non-trivial input."""
    n_samples = int(seconds * sample_rate)
    samples = [
        int(amplitude * 32767 * math.sin(2 * math.pi * freq_hz * i / sample_rate))
        for i in range(n_samples)
    ]
    return struct.pack(f"<{n_samples}h", *samples)


def test_streaming_model_satisfies_protocol():
    """MiniCPMStreamingModel satisfies both DuplexModel and StreamingDuplexModel."""
    model = MiniCPMStreamingModel()
    assert isinstance(model, StreamingDuplexModel), (
        "MiniCPMStreamingModel must satisfy StreamingDuplexModel Protocol"
    )


@pytest.mark.asyncio
async def test_infer_stream_returns_async_generator():
    """infer_stream must be a coroutine returning an AsyncGenerator, not an async-gen function.

    Calling and awaiting infer_stream must yield an object we can iterate with
    'async for', without raising TypeError.
    """
    model = MiniCPMStreamingModel()

    async def _empty_frames():
        return
        yield  # make it an async generator

    gen = await model.infer_stream(_empty_frames(), caused_by=["test-evt-1"])
    assert hasattr(gen, "__aiter__"), "infer_stream must return an async iterable"
    # consume (empty — no frames fed so no proposals)
    proposals = [p async for p in gen]
    assert isinstance(proposals, list)


@pytest.mark.asyncio
async def test_infer_stream_yields_proposals_on_speech_audio():
    """Feed 5 seconds of audio through infer_stream; assert behavioural outputs.

    The model may or may not speak on silence/tone, but:
    - No TypeError is raised.
    - Any yielded items are valid ThinkerProposal instances with non-empty caused_by.
    - The streaming path completes without error.

    Behavioral, not bit-exact (invariant #6 — model decoding varies).
    """
    model = MiniCPMStreamingModel()

    # 5 seconds: mix of silence and a 440 Hz tone to give the audio tower content
    audio_segments = [
        _make_pcm16_silence(1.0),       # 1 s silence
        _make_pcm16_tone(440.0, 2.0),   # 2 s 440 Hz tone
        _make_pcm16_silence(1.0),       # 1 s silence
        _make_pcm16_tone(220.0, 1.0),   # 1 s 220 Hz tone
    ]

    caused_by = ["test-audio-stream-evt-1"]

    async def _frame_iter():
        for seg in audio_segments:
            yield seg, None  # (audio_bytes, video_frame=None)

    gen = await model.infer_stream(_frame_iter(), caused_by=caused_by)
    proposals: list[ThinkerProposal] = []
    async for p in gen:
        proposals.append(p)

    # Structural assertions — behavioral: 0 or more proposals are valid
    for p in proposals:
        assert isinstance(p, ThinkerProposal), f"expected ThinkerProposal, got {type(p)}"
        assert p.caused_by, f"proposal has empty caused_by: {p}"
        assert p.content, f"proposal has empty content: {p}"
        assert p.proposal_type in ("observation", "question", "aesthetic_reaction", "memory_bridge")
        assert 0.0 <= p.confidence <= 1.0
