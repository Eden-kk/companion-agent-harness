"""BackchannelClassifier — short-vocalization backchannel detector stub (v0.1b Task 8).

Classifies short user vocalizations ("yeah", "mm-hmm") during assistant speech
as backchannels vs genuine interruptions. Emits a TurnSignal with a real
`p_backchannel` value (and low `p_done`).

## Adapter-first design

The real lightweight model is injected via the `BackchannelModel` Protocol —
this module never imports torch, onnxruntime, or pipecat. Tests inject a fake
that returns scripted probabilities. The full implementation (v0.1b Task 9)
slots the real model behind this Protocol seam without changing callers.

## Interface semantics

`BackchannelClassifier` exposes `process_frame(frame, caused_by) -> TurnSignal | None`
— the same interface as `VADDetector` and `SmartTurnDetector` — so
`TurnDetectorSuite` can fan frames to all detectors uniformly.

`p_done` is populated as `1.0 - p_backchannel`; `p_continue` is 0.0 (this
detector does not classify end-of-turn — that is SmartTurnDetector's job).
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, TurnSignal

__all__ = ["BackchannelModel", "BackchannelClassifier"]


@runtime_checkable
class BackchannelModel(Protocol):
    """Injected interface for a frame-level backchannel probability model.

    The real implementation wraps a lightweight classifier (v0.1b Task 9).
    Tests inject a fake that returns scripted probabilities.

    Receives one audio frame; returns p_backchannel in [0.0, 1.0].
    """

    def __call__(self, frame: bytes) -> float: ...


class BackchannelClassifier:
    """Classifies short user vocalizations as backchannels.

    On each frame: calls the injected BackchannelModel, logs the invocation
    (invariant #1), and returns a TurnSignal populated with p_backchannel.
    Full implementation in v0.1b Task 9.
    """

    SOURCE = "backchannel_classifier"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        model: BackchannelModel,
        session_id: str,
        logger: EventLogger,
        *,
        frame_duration_ms: int = 32,
    ) -> None:
        self._model = model
        self._session_id = session_id
        self._logger = logger
        self._frame_duration_ms = frame_duration_ms
        self._seq = 0

    def process_frame(self, frame: bytes, caused_by: list[str]) -> TurnSignal | None:
        """Feed one audio frame through the backchannel model.

        Logs a backchannel_frame event for every frame (invariant #1).
        Returns a TurnSignal with p_backchannel populated on every frame.
        """
        p_backchannel = self._model(frame)
        frame_evt = self._emit("backchannel_frame", caused_by, p_backchannel)
        return TurnSignal(
            detector="backchannel",
            p_done=1.0 - p_backchannel,
            p_continue=0.0,
            p_backchannel=p_backchannel,
            confidence=p_backchannel,
            evidence_event_ids=[frame_evt.event_id],
        )

    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, event_type: str, caused_by: list[str], value: float) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-backchannel-{seq}-{now_ms}"
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
