"""SmartTurnDetector — end-of-turn / thinking-pause classifier stub (v0.1b Task 2).

## Interface semantics (ADR note)

`SmartTurnDetector` exposes the same `process_frame(frame, caused_by) -> TurnSignal | None`
interface as `VADDetector`, so `TurnDetectorSuite` can fan frames to all detectors
uniformly.

Unlike `VADDetector` (which may emit a TurnSignal on any frame where silence
has persisted long enough), `SmartTurnDetector` has different internal behavior:
- It accumulates the current-turn audio into an internal buffer on every frame.
- It only invokes the model and potentially emits a `TurnSignal` at
  silence-candidate moments (i.e., when frame energy stays below the RMS
  silence threshold for >= silence_onset_ms of continuous silence).
- Between silence candidates the method returns None without touching the model.

"Silence-candidate" detection is self-contained: each frame's RMS energy is
compared against `silence_rms_threshold` (default 100 on int16 scale). When
accumulated silence exceeds `silence_onset_ms` (default 300 ms) this module
fires the Smart Turn model on the buffered audio — exactly once per candidate
window, not on every frame (watch-item 17).

This "accumulate-internally, emit-at-silence-candidate" design is required
because the Smart Turn v3 model classifies full-turn context (not single frames).
`p_backchannel` is always 0.0 here — that field is populated by `BackchannelClassifier`.

The real Smart Turn v3 model (Pipecat, ~8 MB ONNX, ~12 ms CPU) is injected via
the `SmartTurnModel` Protocol. This module never imports torch, onnxruntime, or
pipecat — the Protocol is the seam where the real model plugs in (v0.1b Task 3).
"""

from __future__ import annotations

import array
import hashlib
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, TurnSignal

__all__ = ["SmartTurnModel", "SmartTurnDetector"]

# Module-level defaults; override via constructor keyword args.
_SILENCE_ONSET_MS: int = 300
_SILENCE_RMS_THRESHOLD: int = 100  # int16 RMS below which a frame counts as silence


@runtime_checkable
class SmartTurnModel(Protocol):
    """Injected interface for a turn-context end-of-turn classifier.

    The real implementation wraps Pipecat Smart Turn v3 (ONNX, CPU).
    Tests inject a fake that returns scripted (p_done, p_continue) pairs.

    Receives the accumulated turn audio buffer; returns probabilities.
    """

    def __call__(self, audio_buffer: bytes) -> tuple[float, float]: ...


class SmartTurnDetector:
    """Accumulates turn audio and emits TurnSignals at silence-candidate moments.

    On each frame: buffer is extended. A silence-candidate fires when frame
    RMS energy stays below `silence_rms_threshold` for >= `silence_onset_ms`
    of continuous silence. At that moment the injected SmartTurnModel is called
    on the full buffer and a TurnSignal is always returned; if `p_done > p_continue`
    the buffer resets (end-of-turn confirmed), otherwise it is retained (thinking pause).

    The model is invoked ONLY at silence-candidate moments — never on every
    frame (watch-item 17). Non-candidate frames emit no event.

    p_backchannel is always 0.0; BackchannelClassifier owns that field.
    """

    SOURCE = "smart_turn_detector"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        model: SmartTurnModel,
        session_id: str,
        logger: EventLogger,
        *,
        silence_onset_ms: int = _SILENCE_ONSET_MS,
        silence_rms_threshold: int = _SILENCE_RMS_THRESHOLD,
        frame_duration_ms: int = 32,
    ) -> None:
        self._model = model
        self._session_id = session_id
        self._logger = logger
        self._silence_onset_ms = silence_onset_ms
        self._silence_rms_threshold = silence_rms_threshold
        self._frame_duration_ms = frame_duration_ms

        self._seq = 0
        self._audio_buffer: bytes = b""
        self._in_speech = False
        self._silence_ms = 0
        self._candidate_fired = False  # True once the model fires for current silence window

    # ------------------------------------------------------------------

    def process_frame(self, frame: bytes, caused_by: list[str]) -> TurnSignal | None:
        """Feed one audio frame; return a TurnSignal at silence-candidate moments.

        Non-candidate frames: buffer is extended, no event is emitted, None returned.
        Silence-candidate moment (first frame after silence_onset_ms of continuous
        silence): model is invoked, invocation event is logged, TurnSignal returned
        if p_done > threshold (buffer reset on end-of-turn).
        """
        self._audio_buffer += frame

        if _frame_rms(frame) >= self._silence_rms_threshold:
            self._in_speech = True
            self._silence_ms = 0
            self._candidate_fired = False
            return None

        if not self._in_speech:
            # Haven't entered speech yet; no candidate to fire.
            return None

        self._silence_ms += self._frame_duration_ms

        if self._silence_ms < self._silence_onset_ms or self._candidate_fired:
            return None

        # First frame that crosses the silence_onset_ms threshold: silence candidate.
        self._candidate_fired = True

        p_done, p_continue = self._model(self._audio_buffer)
        invocation_evt = self._emit("smart_turn_invocation", caused_by, p_done, p_continue)

        signal = TurnSignal(
            detector="smart_turn",
            p_done=p_done,
            p_continue=p_continue,
            p_backchannel=0.0,
            confidence=p_done,
            evidence_event_ids=[invocation_evt.event_id],
        )
        self._emit("smart_turn_signal", [invocation_evt.event_id], p_done, p_continue)
        if p_done > p_continue:
            # End-of-turn confirmed: reset buffer for next utterance.
            self._audio_buffer = b""
            self._in_speech = False
            self._silence_ms = 0
            self._candidate_fired = False
        return signal

    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(
        self, event_type: str, caused_by: list[str], p_done: float, p_continue: float
    ) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-smart-turn-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}:{p_done:.4f}:{p_continue:.4f}".encode()
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


def _frame_rms(frame: bytes) -> float:
    """Return RMS of frame interpreted as little-endian int16 PCM."""
    if len(frame) < 2:
        return 0.0
    samples = array.array("h", frame[: len(frame) - len(frame) % 2])
    return (sum(s * s for s in samples) / len(samples)) ** 0.5
