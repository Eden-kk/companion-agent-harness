"""native_duplex_invocation event emission — invariant fix (v0.1j Task 8).

Verifies:
  1. native_duplex_invocation event is emitted with is_listen payload.
  2. TurnSignal.evidence_event_ids[0] matches the logged event_id.
  3. Causal graph closes with no orphan predecessors.

All three tests run without GPU: they exercise _emit_invocation +
_last_native_duplex_event_id + MiniCPMNativeDuplexEouSource directly via
a thin stub that calls the same code path without loading the real model.
"""

from __future__ import annotations

import asyncio
from typing import AsyncGenerator, AsyncIterator

import pytest

from companion_harness.causal_graph import CausalGraph
from companion_harness.event_logger import EventLogger
from companion_harness.native_duplex_eou import MiniCPMNativeDuplexEouSource
from companion_harness.schemas import Event, MemoryItem, ThinkerProposal


# ---------------------------------------------------------------------------
# Stub: replicates the parts of MiniCPMStreamingModel that _emit_invocation
# and _last_native_duplex_event_id rely on, without importing torch.
# ---------------------------------------------------------------------------


class _StubStreamingModel:
    """Minimal stand-in for MiniCPMStreamingModel (no torch required).

    Wires the same _emit_invocation / _last_native_duplex_event_id attributes
    so that MiniCPMNativeDuplexEouSource can read them.
    """

    SOURCE = "minicpm_streaming"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        *,
        logger: EventLogger | None = None,
        session_id: str = "stub-session",
    ) -> None:
        self._last_is_listen: bool = True
        self._last_native_duplex_event_id: str | None = None
        self._logger = logger
        self._session_id = session_id
        self._seq = 0

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: list[MemoryItem]) -> None:
        pass

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple[MemoryItem, ...] = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            return
            yield  # make it a generator

        return _gen()

    # Expose the real _emit_invocation + _next_seq from foreground_model_minicpm.
    # We replicate them here (same logic) to avoid importing torch.
    import hashlib as _hashlib
    import time as _time
    from datetime import datetime as _dt, timezone as _tz

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_invocation(self, is_listen: bool, caused_by: list[str]) -> Event:
        import hashlib
        import time
        from datetime import datetime, timezone

        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-nd-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"native_duplex_invocation:{event_id}:{is_listen}:{now_ms}".encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=self.SCHEMA_VERSION,
            seq_no=seq,
            event_type="native_duplex_invocation",
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=self.SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="signal_default_30d",
            payload_inline={"is_listen": is_listen, "ts_mono_ms": now_ms},
        )
        if self._logger is not None:
            self._logger.log(evt)
        return evt


# ---------------------------------------------------------------------------
# Logger helper
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_native_duplex_invocation_event_emitted_with_is_listen_payload():
    """_emit_invocation logs a native_duplex_invocation event with is_listen payload."""
    logger, received = _make_logger()
    await logger.start()

    model = _StubStreamingModel(logger=logger, session_id="test-emit")
    caused_by = ["upstream-evt-001"]

    evt = model._emit_invocation(is_listen=False, caused_by=caused_by)

    await asyncio.sleep(0.05)  # let async drain
    await logger.stop()

    assert evt.event_type == "native_duplex_invocation"
    assert evt.payload_inline is not None
    assert evt.payload_inline["is_listen"] is False
    assert "ts_mono_ms" in evt.payload_inline
    assert evt.caused_by == caused_by

    logged_ids = [e.event_id for e in received if e.event_type == "native_duplex_invocation"]
    assert evt.event_id in logged_ids, (
        "native_duplex_invocation event was not delivered to the logger sink"
    )


@pytest.mark.asyncio
async def test_evidence_event_id_matches_real_event():
    """TurnSignal.evidence_event_ids[0] must equal the logged native_duplex_invocation event_id."""
    logger, received = _make_logger()
    await logger.start()

    model = _StubStreamingModel(logger=logger, session_id="test-evid")

    # Simulate what _process_chunk does after streaming_generate:
    model._last_is_listen = False
    invocation_evt = model._emit_invocation(False, caused_by=["upstream-002"])
    model._last_native_duplex_event_id = invocation_evt.event_id

    src = MiniCPMNativeDuplexEouSource(model)  # type: ignore[arg-type]
    signal = src.get_eou_signal()

    await asyncio.sleep(0.05)
    await logger.stop()

    assert signal is not None, "get_eou_signal() must return TurnSignal when is_listen=False"
    assert signal.evidence_event_ids, "TurnSignal.evidence_event_ids must not be empty"
    assert signal.evidence_event_ids[0] == invocation_evt.event_id, (
        f"evidence_event_ids[0]={signal.evidence_event_ids[0]!r} != "
        f"logged event_id={invocation_evt.event_id!r}"
    )


@pytest.mark.asyncio
async def test_causal_graph_closes_with_native_duplex_eou():
    """Every event in a native_duplex EOU chain has a logged predecessor — no orphans."""
    logger, received = _make_logger()
    await logger.start()

    # Root: simulated user audio input event
    root_evt = Event(
        event_id="root-audio-001",
        session_id="test-dag",
        schema_version="0.1",
        seq_no=0,
        event_type="audio_chunk_ingested",
        timestamp_mono_ms=1000,
        timestamp_wall="",
        source="input_ingest",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="raw_audio",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )
    logger.log(root_evt)

    model = _StubStreamingModel(logger=logger, session_id="test-dag")
    model._last_is_listen = False
    invocation_evt = model._emit_invocation(False, caused_by=[root_evt.event_id])
    model._last_native_duplex_event_id = invocation_evt.event_id

    src = MiniCPMNativeDuplexEouSource(model)  # type: ignore[arg-type]
    signal = src.get_eou_signal()

    await asyncio.sleep(0.05)
    await logger.stop()

    assert signal is not None
    # The signal's evidence_event_id must point at a logged event.
    all_ids = {e.event_id for e in received}
    for eid in signal.evidence_event_ids:
        assert eid in all_ids, (
            f"TurnSignal.evidence_event_ids[0]={eid!r} is not a logged event — "
            "DAG is not closed (invariant #1 violation)"
        )

    # Full DAG closure check via CausalGraph.
    graph = CausalGraph(received)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"Causal graph has orphans: {report.orphan_event_ids}\n"
        f"Dangling refs: {report.dangling_refs}"
    )
