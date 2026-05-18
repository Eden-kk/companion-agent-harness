"""Async, non-blocking event logger + replay infrastructure.

See docs/architecture-v0.1.md §Part 2 invariant #10 (logger never blocks the
realtime path; emits log_drop_or_degrade on backpressure) and §Part 6
Stage 0 (replay + causal provenance contract tests).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Awaitable

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
        self._dropped = 0
        self._subscribers: list[Sink] = []
        self._sub_tasks: set[asyncio.Task[None]] = set()

    def subscribe(self, callback: Sink) -> None:
        if self._task is not None:
            raise RuntimeError("subscribe() must be called before EventLogger.start()")
        self._subscribers.append(callback)

    def late_subscribe(self, callback: Sink) -> None:
        """Subscribe after the logger has started.

        Receives all NEW events from the moment of subscription onward;
        historical events are not replayed. Safe to call while _drain is
        running: list.append is atomic in CPython and the drain loop iterates
        a snapshot-free reference, so no lock is needed under asyncio's
        single-threaded event loop.
        """
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Sink) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def log(self, event: Event) -> None:
        """Enqueue event without blocking. Counts drops for degrade flushing by drain loop."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._drain())

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
        if self._sub_tasks:
            await asyncio.gather(*self._sub_tasks, return_exceptions=True)

    async def _drain(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._sink(event)
                for sub in self._subscribers:
                    t = asyncio.create_task(self._safe_subscriber_call(sub, event))
                    self._sub_tasks.add(t)
                    t.add_done_callback(self._sub_tasks.discard)
            finally:
                self._queue.task_done()
            if self._dropped > 0:
                count, self._dropped = self._dropped, 0
                try:
                    await self._sink(self._make_degrade_event(count))
                except Exception:
                    pass

    async def _safe_subscriber_call(self, sub: Sink, event: Event) -> None:
        try:
            await sub(event)
        except Exception as exc:
            import traceback
            print(f"EventLogger subscriber {sub!r} failed: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)

    def _make_degrade_event(self, dropped_count: int) -> Event:
        self._seq += 1
        now_ms = int(time.monotonic() * 1000)
        return Event(
            event_id=f"degrade-{now_ms}-{self._seq}",
            session_id="",
            schema_version="0.1",
            seq_no=self._seq,
            event_type="log_drop_or_degrade",
            timestamp_mono_ms=now_ms,
            timestamp_wall="",
            source="event_logger",
            caused_by=["_dropped_before_enqueue"],
            payload_hash="",
            payload_ref=None,
            payload_kind="signal",
            subject_class="unknown",
            sensitivity="safe",
            retention_policy_id="default",
        )
