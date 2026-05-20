"""PR4 success-criterion tests: background-think injection plumbing.

Two CPU tests:
  (a) Orchestrator-level: ContinuousOrchestrator injects scratchpad and emits
      background_think_injected with correct payload; regression for null source
      (PR3c behaviour preserved).
  (b) Adapter drain test: MiniCPMStreamingModel._drain_scratchpad flushes the
      deque via streaming_prefill(text_list=[...]).  Guard: pytest.importorskip("torch").
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest

from companion_harness.continuous_orchestrator import ContinuousOrchestrator
from companion_harness.evals.scenarios.audio_feeder import DirectAudioInputFeeder
from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

_CHUNK_SAMPLES = 16_000
_BYTES_PER_SAMPLE = 2  # int16


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


class _FakeForegroundModel:
    """stream_chunks mirror + inject_scratchpad recorder."""

    def __init__(self) -> None:
        self.injected: list[str] = []

    def inject_scratchpad(self, text: str) -> None:
        self.injected.append(text)

    async def stream_chunks(
        self,
        audio_in: "asyncio.Queue[tuple[bytes, str]]",
    ) -> AsyncGenerator[tuple[bool, str, None, str], None]:
        buf_samples = 0
        latest_evt_id: str = ""
        while True:
            pcm_bytes, evt_id = await audio_in.get()
            if pcm_bytes == b"" and evt_id == "":
                if buf_samples > 0 and latest_evt_id:
                    yield (True, "", None, latest_evt_id)
                return
            latest_evt_id = evt_id
            buf_samples += len(pcm_bytes) // _BYTES_PER_SAMPLE
            while buf_samples >= _CHUNK_SAMPLES:
                buf_samples -= _CHUNK_SAMPLES
                yield (True, "", None, latest_evt_id)


class _NullAudioOutput:
    @property
    def is_playing(self) -> bool:
        return False

    def start_generation(self, caused_by: list[str]) -> str:
        return "noop"

    def request_stop(self, caused_by: list[str]) -> str:
        return "noop"


def _log_raw_audio_events(
    logger: EventLogger,
    session_id: str,
    feeder_event_ids: list[str],
    chunks: list[bytes],
) -> set[str]:
    logged: set[str] = set()
    for seq_i, (eid, pcm) in enumerate(zip(feeder_event_ids, chunks)):
        raw_evt = Event(
            event_id=eid,
            session_id=session_id,
            schema_version="0.1",
            seq_no=seq_i + 1,
            event_type="raw_audio_chunk",
            timestamp_mono_ms=int(eid.split("-")[2]),
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source="fixture.audio",
            caused_by=[session_id + "-open"],
            payload_hash=hashlib.sha256(pcm).hexdigest()[:16],
            payload_ref=None,
            payload_kind="raw_audio",
            subject_class="self",
            sensitivity="sensitive",
            retention_policy_id="default",
        )
        logger.log(raw_evt)
        logged.add(eid)
    return logged


# ---------------------------------------------------------------------------
# (a) Orchestrator test (no torch)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_thought_injected_on_matching_chunk() -> None:
    """Exactly one inject_scratchpad + one background_think_injected event; DAG closed; text absent."""
    THOUGHT = "user seems distracted today"
    n_chunks = 3

    logger, received = _make_logger()
    await logger.start()

    session_id = "pr4-test-01"
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)
    fake_model = _FakeForegroundModel()

    call_count = 0

    class _OnceThoughtSource:
        def pending_thought(self) -> str | None:
            nonlocal call_count
            call_count += 1
            return THOUGHT if call_count == 2 else None

    orch = ContinuousOrchestrator(
        session_id=session_id,
        logger=logger,
        audio_in=audio_in,
        foreground_model=fake_model,
        audio_output=_NullAudioOutput(),
        thought_source=_OnceThoughtSource(),
    )

    clock = SyntheticClock()
    feeder = DirectAudioInputFeeder(audio_in)

    chunk_bytes = b"\x00" * (_CHUNK_SAMPLES * _BYTES_PER_SAMPLE)
    chunks = [chunk_bytes] * n_chunks

    feeder_event_ids: list[str] = []
    t_ms = clock.now_ms()
    for i in range(n_chunks):
        feeder_event_ids.append(f"fixture-audio-{t_ms}-{i}")
        t_ms += 20

    logged_audio_ids = _log_raw_audio_events(logger, session_id, feeder_event_ids, chunks)

    feeder.feed(chunks, clock)
    audio_in.put_nowait((b"", ""))

    await orch.run()
    await logger.stop()

    # Exactly one poll per chunk (pins the one-poll-per-chunk contract)
    assert call_count == n_chunks, f"expected one poll per chunk, got {call_count}"

    # Exactly one inject_scratchpad call with the correct text
    assert fake_model.injected == [THOUGHT], f"injected={fake_model.injected!r}"

    # Exactly one background_think_injected event
    bti_events = [e for e in received if e.event_type == "background_think_injected"]
    assert len(bti_events) == 1, f"expected 1 bti event, got {len(bti_events)}"

    bti = bti_events[0]
    assert bti.payload_inline is not None
    assert bti.payload_inline["role"] == "background-think"
    assert bti.payload_inline["n_chars"] == len(THOUGHT)

    # DAG: caused_by[0] resolves to a logged raw_audio_chunk event_id
    assert bti.caused_by, f"orphan bti event {bti.event_id}"
    assert bti.caused_by[0] in logged_audio_ids, (
        f"bti caused_by[0]={bti.caused_by[0]!r} not in logged audio ids {logged_audio_ids}"
    )

    # Thought text must not appear in any event payload
    for evt in received:
        payload_str = str(evt.payload_inline)
        assert THOUGHT not in payload_str, (
            f"thought text leaked into event {evt.event_id}: {payload_str!r}"
        )


@pytest.mark.asyncio
async def test_null_thought_source_no_injection() -> None:
    """Default (no thought_source) → zero inject_scratchpad calls; zero bti events."""
    logger, received = _make_logger()
    await logger.start()

    session_id = "pr4-test-02"
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)
    fake_model = _FakeForegroundModel()

    orch = ContinuousOrchestrator(
        session_id=session_id,
        logger=logger,
        audio_in=audio_in,
        foreground_model=fake_model,
        audio_output=_NullAudioOutput(),
        # no thought_source → _NULL_THOUGHT_SOURCE
    )

    clock = SyntheticClock()
    feeder = DirectAudioInputFeeder(audio_in)

    n_chunks = 3
    chunk_bytes = b"\x00" * (_CHUNK_SAMPLES * _BYTES_PER_SAMPLE)
    chunks = [chunk_bytes] * n_chunks

    feeder_event_ids: list[str] = []
    t_ms = clock.now_ms()
    for i in range(n_chunks):
        feeder_event_ids.append(f"fixture-audio-{t_ms}-{i}")
        t_ms += 20

    _log_raw_audio_events(logger, session_id, feeder_event_ids, chunks)
    feeder.feed(chunks, clock)
    audio_in.put_nowait((b"", ""))

    await orch.run()
    await logger.stop()

    assert fake_model.injected == [], f"unexpected calls: {fake_model.injected!r}"
    bti_events = [e for e in received if e.event_type == "background_think_injected"]
    assert bti_events == [], f"unexpected bti events: {bti_events!r}"


# ---------------------------------------------------------------------------
# (b) Adapter drain test — guarded by torch
# ---------------------------------------------------------------------------

def test_drain_scratchpad_calls_streaming_prefill() -> None:
    """_drain_scratchpad flushes deque via streaming_prefill(text_list=[...])."""
    pytest.importorskip("torch")
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel  # noqa: PLC0415

    m = object.__new__(MiniCPMStreamingModel)
    m._scratchpad_queue = collections.deque(["thought-x"])

    calls: list[dict] = []

    class _FakeDuplex:
        def streaming_prefill(self, **kwargs: object) -> None:
            calls.append(dict(kwargs))

    m._drain_scratchpad(_FakeDuplex())

    assert calls == [{"text_list": ["thought-x"]}], f"calls={calls!r}"
    assert len(m._scratchpad_queue) == 0, "queue not emptied"
