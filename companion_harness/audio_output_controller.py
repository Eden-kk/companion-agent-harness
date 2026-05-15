"""AudioOutputController — playback lifecycle, barge-in stop, generation cancel.

See docs/architecture-v0.1.md §Part 3 (adapter interfaces) and §Part 5 for the
new event_types this adapter emits (assistant_generation_start,
assistant_audio_buffer_queued/flushed, assistant_audio_stop_requested/completed).
Stop path is gated by §Part 8 v0.1a barge-in latencies.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import AsyncIterable, Callable, Awaitable
from datetime import datetime, timezone

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

__all__ = ["AudioSink", "AudioOutputController"]

# AudioSink is an injected callback — no hardware/SDK dependency here.
AudioSink = Callable[[bytes], Awaitable[None]]


class AudioOutputController:
    """Manages playback lifecycle: generation, buffering, stop-on-barge-in, cancel.

    The actual audio output device is injected as `sink` — an async callable that
    receives raw audio bytes.  This keeps the controller adapter-pure (no SDK imports).

    Lifecycle per utterance:
      start_generation() → queue_buffer() × N → play() → [stop_requested() if barged]
    """

    SOURCE = "audio_output_controller"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        session_id: str,
        logger: EventLogger,
        sink: AudioSink,
    ) -> None:
        self._session_id = session_id
        self._logger = logger
        self._sink = sink
        self._seq = 0
        self._playing = False
        self._stop_event = asyncio.Event()
        self._generation_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start_generation(self, caused_by: list[str]) -> str:
        """Signal that TTS generation has started. Returns the generation event_id."""
        evt = self._emit("assistant_generation_start", caused_by, payload_kind="model_output")
        self._playing = True
        self._stop_event.clear()
        return evt.event_id

    def queue_buffer(self, chunk: bytes, caused_by: list[str]) -> None:
        """Log that an audio buffer chunk has been queued for playback."""
        self._emit("assistant_audio_buffer_queued", caused_by, payload_kind="raw_audio",
                   sensitivity="sensitive")

    def _flush(self, caused_by: list[str]) -> None:
        """Log that the audio buffer has been fully flushed (utterance complete)."""
        self._emit("assistant_audio_buffer_flushed", caused_by, payload_kind="model_output")
        self._playing = False

    def request_stop(self, caused_by: list[str]) -> str:
        """Non-blocking barge-in stop: logs stop_requested and signals drain to halt.

        Returns the stop_requested event_id so the caller can chain caused_by.
        The realtime path returns immediately; stop_completed is emitted async.
        """
        evt = self._emit("assistant_audio_stop_requested", caused_by, payload_kind="signal")
        self._stop_event.set()
        return evt.event_id

    def set_generation_task(self, task: asyncio.Task[None]) -> None:
        """Register the running asyncio generation Task so cancel_generation() can cancel it."""
        self._generation_task = task

    def cancel_generation(self, caused_by: list[str]) -> None:
        """Cancel any in-flight generation task and log it."""
        self._emit("assistant_generation_cancel_requested", caused_by, payload_kind="signal")
        if self._generation_task is not None and not self._generation_task.done():
            self._generation_task.cancel()
            self._generation_task = None
        self._playing = False

    async def play(self, chunks: AsyncIterable[bytes], generation_event_id: str) -> None:
        """Drain chunks to sink as they arrive, stopping early if request_stop() fires.

        Accepts an async generator or any AsyncIterable[bytes] so each chunk is
        forwarded to the sink as it arrives without waiting for the full utterance
        to buffer. Emits assistant_audio_stop_completed if stopped mid-stream,
        or assistant_audio_buffer_flushed when the stream runs to completion.
        """
        async for chunk in chunks:
            if self._stop_event.is_set():
                self._emit(
                    "assistant_audio_stop_completed",
                    [generation_event_id],
                    payload_kind="signal",
                )
                self._playing = False
                return
            await self._sink(chunk)

        self._flush([generation_event_id])

    @property
    def is_playing(self) -> bool:
        return self._playing

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(
        self,
        event_type: str,
        caused_by: list[str],
        payload_kind: str = "signal",
        sensitivity: str = "safe",
    ) -> Event:
        now_ms = int(time.monotonic() * 1000)
        wall = datetime.now(timezone.utc).isoformat()
        seq = self._next_seq()
        event_id = f"{self._session_id}-aoc-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}:{now_ms}".encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=self.SCHEMA_VERSION,
            seq_no=seq,
            event_type=event_type,
            timestamp_mono_ms=now_ms,
            timestamp_wall=wall,
            source=self.SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind=payload_kind,  # type: ignore[arg-type]
            subject_class="self",
            sensitivity=sensitivity,  # type: ignore[arg-type]
            retention_policy_id="default",
        )
        self._logger.log(evt)
        return evt
