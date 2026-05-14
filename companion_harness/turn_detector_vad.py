"""VADDetector — the single end-of-utterance detector enabled in v0.1a.

See docs/architecture-v0.1.md §Part 3 TurnDetectorSuite (v0.1a uses VAD only;
SmartTurn / SemanticEOU / NativeDuplex are deferred to v0.1b) and §Part 8
for the v0.1a adapter scope. Emits TurnSignal per §Part 5.

The actual VAD model (Silero VAD) requires torch and lives on b200.
This module never imports torch — the model is injected via the VADModel
Protocol so the adapter is locally testable without GPU dependencies.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, TurnSignal

__all__ = ["VADModel", "VADDetector"]

# Silero VAD v5 recommended defaults for 16 kHz audio.
_SPEECH_THRESHOLD: float = 0.5
_SILENCE_ONSET_MS: int = 300


@runtime_checkable
class VADModel(Protocol):
    """Injected interface for a frame-level speech probability model.

    The real implementation wraps Silero VAD on b200 (requires torch).
    Tests inject a fake that returns scripted probabilities.
    """

    def __call__(self, frame: bytes) -> float: ...


class VADDetector:
    """Wraps an injected VADModel and emits TurnSignals on end-of-utterance.

    Tracks per-frame speech probability. When a speech segment ends
    (probability drops below threshold for >= silence_onset_ms of continuous
    silence), emits a TurnSignal and logs it via EventLogger (invariant #1).
    """

    SOURCE = "vad_detector"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        model: VADModel,
        session_id: str,
        logger: EventLogger,
        *,
        speech_threshold: float = _SPEECH_THRESHOLD,
        silence_onset_ms: int = _SILENCE_ONSET_MS,
        frame_duration_ms: int = 32,
    ) -> None:
        self._model = model
        self._session_id = session_id
        self._logger = logger
        self._speech_threshold = speech_threshold
        self._silence_onset_ms = silence_onset_ms
        self._frame_duration_ms = frame_duration_ms

        self._seq = 0
        self._in_speech = False
        self._silence_ms = 0

    def process_frame(self, frame: bytes, caused_by: list[str]) -> TurnSignal | None:
        """Feed one audio frame through the VAD model.

        Logs a vad_frame event for every frame (invariant #1).
        Returns a TurnSignal when end-of-utterance is detected, else None.
        """
        p_speech = self._model(frame)
        frame_evt = self._emit("vad_frame", caused_by, p_speech)

        if p_speech >= self._speech_threshold:
            self._in_speech = True
            self._silence_ms = 0
            return None

        if self._in_speech:
            self._silence_ms += self._frame_duration_ms
            if self._silence_ms >= self._silence_onset_ms:
                self._in_speech = False
                self._silence_ms = 0
                p_done = 1.0 - p_speech
                signal = TurnSignal(
                    detector="vad",
                    p_done=p_done,
                    p_continue=p_speech,
                    p_backchannel=0.0,
                    confidence=p_done,
                    evidence_event_ids=[frame_evt.event_id],
                )
                self._emit("vad_turn_signal", [frame_evt.event_id], p_done)
                return signal

        return None

    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, event_type: str, caused_by: list[str], value: float) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-vad-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}:{value:.4f}".encode()
        ).hexdigest()[:16]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=self.SCHEMA_VERSION,
            seq_no=seq,
            event_type=event_type,
            timestamp_mono_ms=now_ms,
            timestamp_wall=datetime.now(timezone.utc).isoformat(),
            source=self.SOURCE,
            caused_by=caused_by,
            payload_hash=payload_hash,
            payload_ref=None,
            payload_kind="signal",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="default",
        )
        self._logger.log(evt)
        return evt
