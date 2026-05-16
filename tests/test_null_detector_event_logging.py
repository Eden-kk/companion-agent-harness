"""G1 regression test: MiniCPMStreamingModel.set_session wires logger so
_emit_invocation events are logged (caused_by orphan gap closed).

Success criterion (G1 fix): ForegroundModel.__init__ calls set_session on
models that support it, so every native_duplex_invocation event that flows
into TurnSignal.evidence_event_ids has a matching logged event.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncGenerator, AsyncIterator

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.schemas import Event, MemoryItem, ThinkerProposal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


# ---------------------------------------------------------------------------
# Fake streaming model that exposes set_session (mimics MiniCPMStreamingModel)
# ---------------------------------------------------------------------------


class _FakeStreamingModelWithSetSession:
    """Fake that mimics MiniCPMStreamingModel's set_session / _emit_invocation API.

    Holds the logger and session_id that were bound via set_session(), and emits
    a 'native_duplex_invocation' event per infer_stream call (same as the real
    model) so the test can assert the event was logged.
    """

    def __init__(self) -> None:
        self._logger = None
        self._session_id = ""
        self._last_native_duplex_event_id: str | None = None
        self._last_is_listen = True
        self._seq = 0

    def set_session(self, session_id: str, logger: EventLogger) -> None:
        self._session_id = session_id
        self._logger = logger
        self._seq = 0
        self._last_native_duplex_event_id = None
        self._last_is_listen = True

    def set_context(self, items: list[MemoryItem]) -> None:
        pass

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple[MemoryItem, ...] = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            self._seq += 1
            now_ms = int(time.monotonic() * 1000)
            evt_id = f"{self._session_id}-nd-{self._seq}-{now_ms}"
            # Simulate _emit_invocation: create and log the event.
            evt = Event(
                event_id=evt_id,
                session_id=self._session_id,
                schema_version="0.1",
                seq_no=self._seq,
                event_type="native_duplex_invocation",
                timestamp_mono_ms=now_ms,
                timestamp_wall="",
                source="minicpm_streaming",
                caused_by=caused_by,
                payload_hash="",
                payload_ref=None,
                payload_kind="signal",
                subject_class="self",
                sensitivity="safe",
                retention_policy_id="signal_default_30d",
            )
            if self._logger is not None:
                self._logger.log(evt)
            self._last_native_duplex_event_id = evt_id
            self._last_is_listen = False
            yield ThinkerProposal(
                proposal_type="observation",
                content="hello",
                trigger="speech",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.3,
                max_utterance_ms=5000,
                cooldown_consumed="speech_turn",
                caused_by=caused_by,
            )
        return _gen()


class _FakeStreamingModelWithoutSetSession:
    """Fake without set_session (old style) — must not crash ForegroundModel.__init__."""

    def set_context(self, items: list[MemoryItem]) -> None:
        pass

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple[MemoryItem, ...] = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            if False:
                yield  # type: ignore[misc]
        return _gen()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_set_session_called_on_model_that_supports_it() -> None:
    """ForegroundModel.__init__ must call set_session when the model supports it."""
    logger, _ = _make_logger()
    model = _FakeStreamingModelWithSetSession()

    # Before ForegroundModel wraps it, logger is None.
    assert model._logger is None
    assert model._session_id == ""

    ForegroundModel(model=model, session_id="sess-abc", logger=logger)

    assert model._logger is logger
    assert model._session_id == "sess-abc"


def test_set_session_not_called_on_model_without_it() -> None:
    """ForegroundModel.__init__ must not crash for models without set_session."""
    logger, _ = _make_logger()
    model = _FakeStreamingModelWithoutSetSession()
    # Must not raise AttributeError.
    ForegroundModel(model=model, session_id="sess-xyz", logger=logger)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_native_duplex_invocation_event_is_logged() -> None:
    """After set_session wiring, infer_stream's native_duplex_invocation event
    must appear in the EventLogger so caused_by refs resolve (G1 closure)."""
    logger, received = _make_logger()
    await logger.start()
    try:
        model = _FakeStreamingModelWithSetSession()
        fm = ForegroundModel(model=model, session_id="sess-g1", logger=logger)

        async def _frame_iter() -> AsyncGenerator[tuple[bytes, bytes | None], None]:
            yield b"\x00" * 64, None

        proposals = []
        async for proposal in fm.process_stream(_frame_iter(), caused_by=["root-evt"]):
            proposals.append(proposal)

        # Drain the logger sink.
        await asyncio.sleep(0.05)

        event_ids = {e.event_id for e in received}
        nd_events = [e for e in received if e.event_type == "native_duplex_invocation"]

        assert len(nd_events) == 1, f"expected 1 native_duplex_invocation, got {len(nd_events)}"
        nd_evt = nd_events[0]

        # The event_id must use the correct session prefix (not empty-string "-nd-").
        assert nd_evt.event_id.startswith("sess-g1-nd-"), (
            f"event_id '{nd_evt.event_id}' should start with 'sess-g1-nd-'"
        )

        # The caused_by reference from downstream events must resolve.
        # (model._last_native_duplex_event_id is what native_duplex_eou would cite)
        cited_id = model._last_native_duplex_event_id
        assert cited_id is not None
        assert cited_id in event_ids, (
            f"caused_by reference '{cited_id}' not found in logged event_ids — "
            "G1 orphan-ref regression"
        )
    finally:
        await logger.stop()
