"""Async, non-blocking event logger + replay infrastructure.

See docs/architecture-v0.1.md §Part 2 invariant #10 (logger never blocks the
realtime path; emits log_drop_or_degrade on backpressure) and §Part 6
Stage 0 (replay + causal provenance contract tests).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Awaitable
from typing import Any

from companion_harness.schemas import Event

__all__ = ["EventLogger"]

Sink = Callable[[Event], Awaitable[None]]


class EventLogger:
    """Async, non-blocking event logger.

    The caller calls `log(event)` on the realtime path — it returns immediately.
    A background task drains the internal queue to the provided sink.

    On backpressure (queue full), a log_drop_or_degrade event is emitted
    in place of the dropped event; the realtime path never blocks.
    """

    def __init__(self, sink: Sink, maxsize: int = 1024) -> None:
        self._sink = sink
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._task: asyncio.Task[None] | None = None
        self._seq = 0

    def log(self, event: Event) -> None:
        """Enqueue event without blocking. Emits log_drop_or_degrade on backpressure."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            degrade = self._make_degrade_event(event)
            try:
                self._queue.put_nowait(degrade)
            except asyncio.QueueFull:
                pass  # queue full even for the degrade notice — silently lose only this notice

    async def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._drain())

    async def stop(self) -> None:
        if self._task is None:
            return
        await self._queue.join()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._sink(event)
            finally:
                self._queue.task_done()

    def _make_degrade_event(self, dropped: Event) -> Event:
        self._seq += 1
        now_ms = int(time.monotonic() * 1000)
        return Event(
            event_id=f"degrade-{now_ms}-{self._seq}",
            session_id=dropped.session_id,
            schema_version=dropped.schema_version,
            seq_no=-1,
            event_type="log_drop_or_degrade",
            timestamp_mono_ms=now_ms,
            timestamp_wall="",
            source="event_logger",
            caused_by=[dropped.event_id],
            payload_hash="",
            payload_ref=None,
            payload_kind="signal",
            subject_class="unknown",
            sensitivity="safe",
            retention_policy_id="default",
        )
