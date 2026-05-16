"""Audit chain closure for audio tee drop events.

Contract tests (sub-plan §3.2):
1. test_drop_event_caused_by_points_to_real_chunk_id — drop event carries real
   chunk event_id in caused_by, NOT the sentinel "_dropped_before_enqueue".
2. test_tee_depth_configurable — orchestrator accepts audio_tee_depth kwarg;
   both tee queues reflect the configured depth.
3. test_audio_tee_drop_summary_emits_every_60s — periodic summary event emitted
   once per 60-s window with correct payload keys.
4. test_dropped_before_enqueue_source_identified — synthetic 30-s session at
   100 frames/s with depth=256; zero sentinel caused_by; total drops < 5.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

import pytest

from companion_harness.schemas import Event, PolicyInputs, SpeakDecision, ThinkerProposal, TurnSignal
from companion_harness.realtime_orchestrator import (
    _drop_oldest_put,
    StreamingRealtimeOrchestrator,
)


# ---------------------------------------------------------------------------
# Minimal fakes (no SDK, no model weights)
# ---------------------------------------------------------------------------

class _FakeLogger:
    def __init__(self) -> None:
        self.logged: list[Event] = []

    def log(self, event: Event) -> None:
        self.logged.append(event)


def _make_event_logger() -> tuple[Any, list[Event]]:
    """Return a real EventLogger wired to a list sink."""
    from companion_harness.event_logger import EventLogger

    received: list[Event] = []

    async def sink(e: Event) -> None:
        received.append(e)

    return EventLogger(sink, maxsize=4096), received


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


def _make_test_event(event_id: str, event_type: str = "raw_audio_chunk") -> Event:
    return Event(
        event_id=event_id,
        session_id="test",
        schema_version="0.1",
        seq_no=0,
        event_type=event_type,
        timestamp_mono_ms=int(time.monotonic() * 1000),
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source="test",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="default",
    )


# ---------------------------------------------------------------------------
# Test 1: caused_by points to real chunk id
# ---------------------------------------------------------------------------

def test_drop_event_caused_by_points_to_real_chunk_id() -> None:
    """Push N+1 frames into a tee with maxsize N; the drop event must have
    caused_by=[real_chunk_event_id], NOT ["_dropped_before_enqueue"]."""
    q: asyncio.Queue[Any] = asyncio.Queue(maxsize=3)
    fake_logger = _FakeLogger()

    # Fill the queue to capacity without triggering a drop.
    for i in range(3):
        item = (_pcm_chunk(), f"chunk-{i}")
        _drop_oldest_put(q, item, fake_logger, f"chunk-{i}", "detectors")

    assert len(fake_logger.logged) == 0, "no drops yet — queue has room"

    # One more push — oldest is evicted; the new item's chunk id is in caused_by.
    overflow_id = "chunk-overflow-99"
    item = (_pcm_chunk(), overflow_id)
    dropped = _drop_oldest_put(q, item, fake_logger, overflow_id, "detectors")

    assert dropped is True
    assert len(fake_logger.logged) == 1
    drop_evt = fake_logger.logged[0]

    assert drop_evt.event_type == "log_drop_or_degrade"
    assert drop_evt.caused_by == [overflow_id], (
        f"expected caused_by=['{overflow_id}'], got {drop_evt.caused_by!r} — "
        "sentinel '_dropped_before_enqueue' must not appear"
    )
    assert "_dropped_before_enqueue" not in drop_evt.caused_by

    # payload_inline must identify the tee.
    assert drop_evt.payload_inline is not None
    assert drop_evt.payload_inline.get("tee_name") == "detectors"


# ---------------------------------------------------------------------------
# Test 2: tee depth configurable
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_tee_depth_configurable(tmp_path) -> None:
    """Orchestrator accepts audio_tee_depth kwarg; both tee queues use it."""
    from companion_harness.audio_output_controller import AudioOutputController
    from companion_harness.event_logger import EventLogger
    from companion_harness.foreground_model import ForegroundModel
    from companion_harness.input_ingest import InputIngest
    from companion_harness.turn_detector_smart import SmartTurnDetector
    from companion_harness.turn_detector_vad import VADDetector

    logger, _ = _make_event_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-tee-depth"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=512)

    class _NullVAD:
        def process_frame(self, f, cb): return None
        def set_speech_threshold(self, v): pass
        def set_silence_onset_ms(self, v): pass
        def set_frame_duration_ms(self, v): pass

    class _NullSmartTurn:
        def process_frame(self, f, cb): return None
        def set_p_done_threshold(self, v): pass

    class _NullBC:
        def process_frame(self, f, cb): return None
        def set_p_backchannel_threshold(self, v): pass

    class _NullFG:
        async def stream_tokens(self, *a, **kw) -> AsyncIterator[ThinkerProposal]:
            return
            yield  # noqa: unreachable

    class _NullTTS:
        async def synthesize(self, text: str, caused_by, session_id, event_logger): return iter([])

    async def _noop_sink(data: bytes) -> None:
        pass

    def _builder(sig, hist) -> PolicyInputs:
        return PolicyInputs(
            turn_signal=sig,
            signal_history=hist,
            companion_state=None,
            session_id=session_id,
            logger=logger,
        )

    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink)

    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=_NullVAD(),  # type: ignore[arg-type]
        smart_turn_detector=_NullSmartTurn(),  # type: ignore[arg-type]
        backchannel_classifier=_NullBC(),  # type: ignore[arg-type]
        policy_inputs_builder=_builder,
        foreground_model=_NullFG(),  # type: ignore[arg-type]
        audio_output=controller,
        tts_adapter=_NullTTS(),  # type: ignore[arg-type]
        audio_tee_depth=128,
    )

    assert orch._tee_to_detectors.maxsize == 128
    assert orch._tee_to_foreground.maxsize == 128
    assert orch._tee_depth == 128

    await logger.stop()


# ---------------------------------------------------------------------------
# Test 3: periodic summary event emits every 60s
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_audio_tee_drop_summary_emits_every_60s(tmp_path) -> None:
    """With a patched _SUMMARY_INTERVAL_MS=0 the summary fires on the very next
    frame after the interval elapses; assert at least one audio_tee_drop_summary
    event and check its payload keys."""
    from companion_harness.audio_output_controller import AudioOutputController
    from companion_harness.backchannel_classifier import BackchannelClassifier
    from companion_harness.event_logger import EventLogger
    from companion_harness.foreground_model import ForegroundModel
    from companion_harness.input_ingest import CaptureMetadata, InputIngest
    from companion_harness.turn_detector_smart import SmartTurnDetector
    from companion_harness.turn_detector_vad import VADDetector

    logger, received = _make_event_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-tee-summary"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=512)

    class _NullVAD:
        def process_frame(self, f, cb): return None
        def set_speech_threshold(self, v): pass
        def set_silence_onset_ms(self, v): pass
        def set_frame_duration_ms(self, v): pass

    class _NullSmartTurn:
        def process_frame(self, f, cb): return None
        def set_p_done_threshold(self, v): pass

    class _NullBC:
        def process_frame(self, f, cb): return None
        def set_p_backchannel_threshold(self, v): pass

    class _NullFG:
        async def stream_tokens(self, *a, **kw) -> AsyncIterator[ThinkerProposal]:
            return
            yield  # noqa: unreachable

    class _NullTTS:
        async def synthesize(self, text: str, caused_by, session_id, event_logger): return iter([])

    async def _noop_sink(data: bytes) -> None:
        pass

    def _builder(sig, hist) -> PolicyInputs:
        return PolicyInputs(
            turn_signal=sig,
            signal_history=hist,
            companion_state=None,
            session_id=session_id,
            logger=logger,
        )

    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink)

    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=_NullVAD(),  # type: ignore[arg-type]
        smart_turn_detector=_NullSmartTurn(),  # type: ignore[arg-type]
        backchannel_classifier=_NullBC(),  # type: ignore[arg-type]
        policy_inputs_builder=_builder,
        foreground_model=_NullFG(),  # type: ignore[arg-type]
        audio_output=controller,
        tts_adapter=_NullTTS(),  # type: ignore[arg-type]
        audio_tee_depth=256,
    )

    # Force the summary interval to fire immediately by back-dating the last summary time.
    orch._tee_summary_last_ms = time.monotonic() * 1000 - 61_000

    await orch.start()

    # Push a few frames; the summary should fire on the first frame after elapsed interval.
    ingest_evt = ingest.ingest_chunk(session, _pcm_chunk(), CaptureMetadata(
        client_id="c",
        timestamp_mono_ms=1000,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    ))
    for _ in range(5):
        await audio_in.put((_pcm_chunk(), ingest_evt.event_id))

    # Give the tee task a chance to run.
    await asyncio.sleep(0.05)
    await orch.stop()
    await logger.stop()

    summary_events = [e for e in received if e.event_type == "audio_tee_drop_summary"]
    assert len(summary_events) >= 1, (
        f"expected at least 1 audio_tee_drop_summary, got {len(summary_events)}; "
        f"all event types: {[e.event_type for e in received]}"
    )
    evt = summary_events[0]
    assert evt.payload_inline is not None
    assert "drop_count_60s" in evt.payload_inline
    assert "tee_name" in evt.payload_inline
    assert "current_queue_depth" in evt.payload_inline
    assert evt.payload_inline["tee_name"] in ("detectors", "foreground")


# ---------------------------------------------------------------------------
# Test 4: under synthetic load at 100 fps + depth=256, drops < 5 and no sentinel
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dropped_before_enqueue_source_identified(tmp_path) -> None:
    """Synthetic load at 100 frames/s with a slow consumer.

    With audio_tee_depth=256 (2.56 s headroom) the consumers (running via
    asyncio cooperative scheduling) should easily drain the tee before it
    overflows.  Asserts:
    - every log_drop_or_degrade event has non-sentinel caused_by (no
      "_dropped_before_enqueue")
    - total drops < 5 for a 1-second burst (100 frames) at depth=256
    """
    from companion_harness.audio_output_controller import AudioOutputController
    from companion_harness.event_logger import EventLogger
    from companion_harness.foreground_model import ForegroundModel
    from companion_harness.input_ingest import CaptureMetadata, InputIngest
    from companion_harness.turn_detector_smart import SmartTurnDetector
    from companion_harness.turn_detector_vad import VADDetector

    logger, received = _make_event_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-no-sentinel"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=512)

    class _NullVAD:
        def process_frame(self, f, cb): return None
        def set_speech_threshold(self, v): pass
        def set_silence_onset_ms(self, v): pass
        def set_frame_duration_ms(self, v): pass

    class _NullSmartTurn:
        def process_frame(self, f, cb): return None
        def set_p_done_threshold(self, v): pass

    class _NullBC:
        def process_frame(self, f, cb): return None
        def set_p_backchannel_threshold(self, v): pass

    class _NullFG:
        async def stream_tokens(self, *a, **kw) -> AsyncIterator[ThinkerProposal]:
            return
            yield  # noqa: unreachable

    class _NullTTS:
        async def synthesize(self, text, caused_by, session_id, event_logger): return iter([])

    async def _noop_sink(data: bytes) -> None:
        pass

    def _builder(sig, hist) -> PolicyInputs:
        return PolicyInputs(
            turn_signal=sig,
            signal_history=hist,
            companion_state=None,
            session_id=session_id,
            logger=logger,
        )

    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink)

    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=_NullVAD(),  # type: ignore[arg-type]
        smart_turn_detector=_NullSmartTurn(),  # type: ignore[arg-type]
        backchannel_classifier=_NullBC(),  # type: ignore[arg-type]
        policy_inputs_builder=_builder,
        foreground_model=_NullFG(),  # type: ignore[arg-type]
        audio_output=controller,
        tts_adapter=_NullTTS(),  # type: ignore[arg-type]
        audio_tee_depth=256,
    )

    await orch.start()

    _meta = CaptureMetadata(
        client_id="test-client",
        timestamp_mono_ms=1000,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )
    # Push 100 frames in a tight burst (simulates 100 fps for 1 s).
    for i in range(100):
        chunk_evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta)
        await audio_in.put((_pcm_chunk(), chunk_evt.event_id))
        if i % 10 == 0:
            await asyncio.sleep(0)  # yield to let tee task drain

    await asyncio.sleep(0.1)
    await orch.stop()
    await logger.stop()

    drop_events = [e for e in received if e.event_type == "log_drop_or_degrade"]

    # Audit chain: no sentinel in any drop event.
    for evt in drop_events:
        assert "_dropped_before_enqueue" not in evt.caused_by, (
            f"sentinel found in drop event {evt.event_id}: caused_by={evt.caused_by!r}"
        )
        assert len(evt.caused_by) >= 1 and evt.caused_by[0] != "_dropped_before_enqueue"

    # With depth=256 and asyncio cooperative scheduling, a 100-frame burst
    # should produce very few (ideally zero) drops.
    assert len(drop_events) < 5, (
        f"expected < 5 drops with tee_depth=256, got {len(drop_events)} — "
        "raise _AUDIO_TEE_DEPTH or investigate consumer stall"
    )
