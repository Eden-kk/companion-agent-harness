"""DirectAudioInputFeeder — pumps audio bytes into orchestrator's audio_in queue.

eval-subsystem-spec.md Anchor 6 (synthetic_clock timing mode).
Phase A.5 Task A.5-2.

Bypasses WebSocket entirely; each chunk is tagged with a deterministic
event_id derived from the clock's current ms timestamp + chunk index.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock


class DirectAudioInputFeeder:
    """Injects prerecorded audio chunks into a queue, no WebSocket required."""

    def __init__(self, audio_in: "asyncio.Queue[tuple[bytes, str]]") -> None:
        self._audio_in = audio_in

    def feed(self, audio_chunks: list[bytes], clock: "SyntheticClock") -> None:
        """Synchronously put each chunk onto the queue tagged with a synthetic event_id.

        Each chunk advances the clock by 20 ms (one standard 16-kHz VAD frame).
        Callers that need a different cadence should advance the clock themselves
        between calls to feed().
        """
        for i, chunk in enumerate(audio_chunks):
            event_id = f"fixture-audio-{clock.now_ms()}-{i}"
            self._audio_in.put_nowait((chunk, event_id))
            clock.advance_ms(20)
