"""Tests for DirectAudioInputFeeder (Phase A.5 Task A.5-2)."""

import asyncio

from companion_harness.evals.scenarios.audio_feeder import DirectAudioInputFeeder
from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock


def _make_queue() -> "asyncio.Queue[tuple[bytes, str]]":
    return asyncio.Queue()


def test_feed_puts_all_chunks_on_queue() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = _make_queue()
    feeder = DirectAudioInputFeeder(q)
    clock = SyntheticClock()
    chunks = [b"chunk0", b"chunk1", b"chunk2"]
    feeder.feed(chunks, clock)
    assert q.qsize() == 3


def test_feed_event_ids_are_unique() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = _make_queue()
    feeder = DirectAudioInputFeeder(q)
    clock = SyntheticClock()
    feeder.feed([b"a", b"b", b"c"], clock)
    items = [q.get_nowait() for _ in range(3)]
    event_ids = [item[1] for item in items]
    assert len(set(event_ids)) == 3


def test_feed_advances_clock_by_20ms_per_chunk() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = _make_queue()
    feeder = DirectAudioInputFeeder(q)
    clock = SyntheticClock()
    feeder.feed([b"x", b"y"], clock)
    # 2 chunks × 20 ms each = 40 ms
    assert clock.now_ms() == 40


def test_feed_empty_chunks_does_nothing() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = _make_queue()
    feeder = DirectAudioInputFeeder(q)
    clock = SyntheticClock()
    feeder.feed([], clock)
    assert q.qsize() == 0
    assert clock.now_ms() == 0


def test_feed_chunk_bytes_preserved_on_queue() -> None:
    q: asyncio.Queue[tuple[bytes, str]] = _make_queue()
    feeder = DirectAudioInputFeeder(q)
    clock = SyntheticClock()
    payload = b"\x00\x01\x02\x03"
    feeder.feed([payload], clock)
    chunk_bytes, _ = q.get_nowait()
    assert chunk_bytes == payload
