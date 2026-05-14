"""SmartTurnDetector — end-of-turn / thinking-pause classifier stub (v0.1b Task 2).

## Interface semantics (ADR note)

`SmartTurnDetector` exposes the same `process_frame(frame, caused_by) -> TurnSignal | None`
interface as `VADDetector`, so `TurnDetectorSuite` can fan frames to all detectors
uniformly.

Unlike `VADDetector` (which may emit a TurnSignal on any frame where silence
has persisted long enough), `SmartTurnDetector` has different internal behavior:
- It accumulates the current-turn audio into an internal buffer on every frame.
- It only invokes the model and potentially emits a `TurnSignal` at
  silence-candidate moments (i.e., when VAD indicates a pause onset).
- Between silence candidates the method returns None without touching the model.

This "accumulate-internally, emit-at-silence-candidate" design is required
because the Smart Turn v3 model classifies full-turn context (not single frames).
`p_backchannel` is always 0.0 here — that field is populated by `BackchannelClassifier`.

The real Smart Turn v3 model (Pipecat, ~8 MB ONNX, ~12 ms CPU) is injected via
the `SmartTurnModel` Protocol. This module never imports torch, onnxruntime, or
pipecat — the Protocol is the seam where the real model plugs in (v0.1b Task 3).
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, TurnSignal

__all__ = ["SmartTurnModel", "SmartTurnDetector"]


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

    On each frame: buffer is extended. At a silence-candidate moment the
    injected SmartTurnModel is called on the full buffer and a TurnSignal
    is returned if p_done exceeds threshold. Every model invocation is
    logged (invariant #1).

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
        silence_candidate_threshold: float = 0.5,
        frame_duration_ms: int = 32,
    ) -> None:
        self._model = model
        self._session_id = session_id
        self._logger = logger
        self._silence_candidate_threshold = silence_candidate_threshold
        self._frame_duration_ms = frame_duration_ms

        self._seq = 0
        self._audio_buffer: bytes = b""

    def process_frame(self, frame: bytes, caused_by: list[str]) -> TurnSignal | None:
        """Feed one audio frame; return a TurnSignal at silence-candidate moments.

        The caller (TurnDetectorSuite) signals a silence candidate by passing
        a frame whose VAD probability is below threshold — the same frame bytes
        used by VADDetector. Stub: silence-candidate detection is a placeholder
        (always treats every frame as a candidate) until Task 3 wires real VAD
        coordination. The accumulation buffer and emission path are present.
        """
        self._audio_buffer += frame

        # TODO (Task 3): receive an explicit silence_candidate flag from
        # TurnDetectorSuite rather than treating every frame as a candidate.
        p_done, p_continue = self._model(self._audio_buffer)
        invocation_evt = self._emit("smart_turn_invocation", caused_by, p_done, p_continue)

        if p_done < self._silence_candidate_threshold:
            return None

        signal = TurnSignal(
            detector="smart_turn",
            p_done=p_done,
            p_continue=p_continue,
            p_backchannel=0.0,
            confidence=p_done,
            evidence_event_ids=[invocation_evt.event_id],
        )
        self._emit("smart_turn_signal", [invocation_evt.event_id], p_done, p_continue)
        self._audio_buffer = b""
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
