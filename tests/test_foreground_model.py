"""ForegroundModel adapter — unit tests.

Success criterion (ROADMAP Task 7): GPU-free tests confirm import cleanliness,
correct event emission, and causal-edge repair for proposals returned with
empty caused_by.

Extended (foundational-duplexmodel-protocol-extension): tests for the
optional video_frame param, StreamingDuplexModel Protocol, and
ForegroundModel.process_stream() logging pass-through.
"""

import subprocess
import sys
from typing import AsyncGenerator, AsyncIterator

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import DuplexModel, ForegroundModel, StreamingDuplexModel
from companion_harness.schemas import Event, ThinkerProposal


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


def _make_proposal(caused_by: list[str] | None = None) -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="observation",
        content="the light shifted",
        trigger="scene_change",
        confidence=0.8,
        novelty=0.7,
        interruption_cost=0.2,
        max_utterance_ms=1200,
        cooldown_consumed="aesthetic_reaction",
        caused_by=caused_by if caused_by is not None else ["upstream-evt-1"],
    )


class _FakeModel:
    """Injected DuplexModel that returns a scripted sequence of proposals."""

    def __init__(self, returns: list[ThinkerProposal | None]) -> None:
        self._returns = iter(returns)

    def infer(
        self, audio_frame: bytes, video_frame: bytes | None = None
    ) -> ThinkerProposal | None:
        return next(self._returns)

    def set_context(self, items) -> None:
        pass


def test_no_torch_import():
    """Importing foreground_model must not transitively pull in torch.

    Checked in a fresh subprocess so the result is independent of what other
    tests (or their fixtures) may have already imported in the current process.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import companion_harness.foreground_model, sys; sys.exit('torch' in sys.modules)",
        ],
        capture_output=True,
    )
    assert result.returncode == 0, "importing foreground_model pulled in torch"


@pytest.mark.asyncio
async def test_none_path_emits_only_frame_event():
    """When the model returns None, only a foreground_frame event is logged — no proposal event."""
    logger, received = _make_logger()
    await logger.start()

    model = _FakeModel([None])
    fm = ForegroundModel(model=model, session_id="sess-none", logger=logger)
    result = fm.process_frame(b"\x00" * 512, caused_by=["input-evt-1"])

    await logger.stop()

    assert result is None
    event_types = [e.event_type for e in received]
    assert event_types == ["foreground_frame"], f"unexpected events: {event_types}"


@pytest.mark.asyncio
async def test_proposal_path_emits_both_events_with_caused_by():
    """When the model returns a ThinkerProposal, both foreground_frame and
    foreground_proposal are logged, and the returned proposal has non-empty caused_by.
    """
    logger, received = _make_logger()
    await logger.start()

    proposal = _make_proposal(caused_by=["upstream-evt-1"])
    model = _FakeModel([proposal])
    fm = ForegroundModel(model=model, session_id="sess-prop", logger=logger)
    result = fm.process_frame(b"\x00" * 512, caused_by=["input-evt-1"])

    await logger.stop()

    assert result is proposal
    assert result.caused_by, "returned proposal must have non-empty caused_by"

    event_types = [e.event_type for e in received]
    assert "foreground_frame" in event_types
    assert "foreground_proposal" in event_types

    frame_evt = next(e for e in received if e.event_type == "foreground_frame")
    proposal_evt = next(e for e in received if e.event_type == "foreground_proposal")

    assert frame_evt.caused_by == ["input-evt-1"]
    assert frame_evt.payload_kind == "raw_audio"
    assert proposal_evt.caused_by == [frame_evt.event_id]
    assert proposal_evt.payload_kind == "model_output"


@pytest.mark.asyncio
async def test_empty_caused_by_repaired_to_frame_event_id():
    """A proposal returned with caused_by=[] is repaired to [frame_evt.event_id]
    so the causal DAG stays closed (invariant #1).
    """
    logger, received = _make_logger()
    await logger.start()

    proposal = _make_proposal(caused_by=[])
    model = _FakeModel([proposal])
    fm = ForegroundModel(model=model, session_id="sess-repair", logger=logger)
    result = fm.process_frame(b"\x00" * 512, caused_by=["input-evt-2"])

    await logger.stop()

    frame_evt = next(e for e in received if e.event_type == "foreground_frame")
    assert result is not None
    assert result.caused_by == [frame_evt.event_id], (
        f"expected caused_by repaired to [{frame_evt.event_id!r}], got {result.caused_by}"
    )


# ---------------------------------------------------------------------------
# Protocol-extension tests (foundational-duplexmodel-protocol-extension)
# ---------------------------------------------------------------------------


def test_audio_only_fake_satisfies_duplex_model_protocol():
    """An audio-only fake (no infer_stream, no video_frame) still satisfies DuplexModel.

    This is the key invariant: adding optional video_frame and the separate
    StreamingDuplexModel Protocol must not break existing audio-only fakes.
    """
    assert isinstance(_FakeModel([None]), DuplexModel)
    assert not isinstance(_FakeModel([None]), StreamingDuplexModel)


def test_streaming_fake_satisfies_both_protocols():
    """A fake that implements infer_stream satisfies both DuplexModel and StreamingDuplexModel."""

    class _FakeStreamingModel:
        def infer(
            self, audio_frame: bytes, video_frame: bytes | None = None
        ) -> ThinkerProposal | None:
            return None

        def set_context(self, items) -> None:
            pass

        async def infer_stream(
            self,
            frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
            caused_by: list[str],
            context_items: tuple = (),
        ) -> AsyncGenerator[ThinkerProposal, None]:
            async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
                yield _make_proposal(caused_by=caused_by)

            return _gen()

    fake = _FakeStreamingModel()
    assert isinstance(fake, DuplexModel)
    assert isinstance(fake, StreamingDuplexModel)


@pytest.mark.asyncio
async def test_process_stream_proposals_carry_caused_by():
    """process_stream yields ThinkerProposals that all carry non-empty caused_by.

    The streaming logging pass-through must satisfy invariant #1: every event
    (foreground_frame + foreground_proposal) is emitted, and every yielded
    proposal has a non-empty caused_by[] pointing into the logged DAG.
    """

    class _FakeStreamingModel:
        def infer(
            self, audio_frame: bytes, video_frame: bytes | None = None
        ) -> ThinkerProposal | None:
            return None

        def set_context(self, items) -> None:
            pass

        async def infer_stream(
            self,
            frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
            caused_by: list[str],
            context_items: tuple = (),
        ) -> AsyncGenerator[ThinkerProposal, None]:
            async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
                yield _make_proposal(caused_by=caused_by)
                yield _make_proposal(caused_by=[])  # empty caused_by: will be repaired

            return _gen()

    logger, received = _make_logger()
    await logger.start()

    fm = ForegroundModel(
        model=_FakeStreamingModel(),  # type: ignore[arg-type]
        session_id="sess-stream",
        logger=logger,
    )

    async def _frames() -> AsyncGenerator[tuple[bytes, bytes | None], None]:
        yield b"\x00" * 512, None

    proposals: list[ThinkerProposal] = []
    async for p in fm.process_stream(_frames(), caused_by=["input-stream-evt-1"]):
        proposals.append(p)

    await logger.stop()

    assert len(proposals) == 2, f"expected 2 proposals, got {len(proposals)}"
    for p in proposals:
        assert p.caused_by, f"proposal has empty caused_by: {p}"

    event_types = [e.event_type for e in received]
    assert "foreground_frame" in event_types
    assert event_types.count("foreground_proposal") == 2
