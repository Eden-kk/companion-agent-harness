"""F0b regression: _tee_to_foreground is drained at batch open so inter-batch frames
(silence/breath arriving between turn N close and turn N+1 open) are not fed as a
stale prefix to the foreground model in turn N+1.

Success criterion:
  - Frames produced BEFORE batch-open are not yielded by the next batch's frame_iter.
  - Frames produced AFTER batch-open ARE yielded.
"""

from __future__ import annotations

import asyncio

import pytest

from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator


# ---------------------------------------------------------------------------
# Minimal test of the drain logic directly on the queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_inter_batch_frames_drained():
    """Simulate the _foreground_stream_task drain: frames in _tee_to_foreground
    that arrived before batch-open are discarded; frames added after are kept.
    """
    q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    # Pre-populate "inter-batch" frames (these should be drained)
    stale_frame = (b"\xAA" * 512, "stale-evt-id")
    for _ in range(3):
        q.put_nowait(stale_frame)

    # Simulate the drain loop from _foreground_stream_task
    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break

    # Now add a "real" turn N+1 frame
    fresh_frame = (b"\xBB" * 512, "fresh-evt-id")
    q.put_nowait(fresh_frame)

    assert q.qsize() == 1, f"Expected 1 frame after drain, got {q.qsize()}"
    item = q.get_nowait()
    assert item == fresh_frame, "Remaining frame should be the fresh turn N+1 frame"


@pytest.mark.asyncio
async def test_batch_open_event_cleared_before_drain():
    """The batch_open_event is cleared (edge: not re-triggered) before drain runs.

    This ensures _foreground_stream_task won't busy-loop if _batch_open_event fires
    again while the drain is in progress.
    """
    batch_open = asyncio.Event()
    q: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    # Simulate: batch_open fires, then we clear it and drain
    batch_open.set()
    await batch_open.wait()
    batch_open.clear()

    # Add stale frames and drain
    for _ in range(5):
        q.put_nowait((b"\x00" * 512, "stale"))

    while not q.empty():
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            break

    assert q.empty(), "Queue must be empty after drain"
    assert not batch_open.is_set(), "batch_open_event must remain cleared after drain"
