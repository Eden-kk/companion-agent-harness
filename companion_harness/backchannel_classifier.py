"""BackchannelClassifier — short-vocalization backchannel detector (v0.1b Task 9).

Classifies short user vocalizations ("yeah", "mm-hmm") occurring during
assistant speech as backchannels vs genuine interruptions. Emits a TurnSignal
with `p_backchannel` populated from the injected model on every frame.

## Adapter-first design

The real lightweight model is injected via the `BackchannelModel` Protocol —
this module never imports torch, onnxruntime, or pipecat. Tests inject a fake
that returns scripted probabilities.

## Interface semantics

`BackchannelClassifier` exposes `process_frame(frame, caused_by) -> TurnSignal | None`
— the same interface as `VADDetector` and `SmartTurnDetector` — so
`TurnDetectorSuite` can fan frames to all detectors uniformly.

## Invocation cadence (WI-19)

The model is invoked on **every frame** — not energy-gated. Backchannels
("yeah", "mm-hmm") are 1–2 frames (32–64 ms) of speech energy. An
energy gate fires on silence; backchannel vocalizations are the opposite
— brief bursts of energy during assistant speech. Gating on silence would
miss them entirely. SmartTurnDetector uses silence-candidate gating because
it needs turn-context; BackchannelClassifier needs the vocalization frame
itself, so every-frame is the correct cadence.

## p_done / p_continue values (WI-18)

`p_done` and `p_continue` are set to independent, defensible constants:
- `p_done = 0.05`: a backchannel is not an end-of-turn signal. The near-zero
  value acknowledges that the vocalization has negligible turn-completion
  probability; it is not forced to 0.0 because we cannot rule out that any
  brief vocalization carries trace EOU signal.
- `p_continue = 0.10`: slightly above `p_done` because the user is mid-stream
  (actively deferring the floor back to the assistant), but still low — they
  are not asserting an intent to continue speaking. These values are NOT
  derived from `p_backchannel`; they represent the EOU state orthogonally.
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


_P_DONE: float = 0.05  # backchannel is not an EOU signal; see module docstring WI-18
_P_CONTINUE: float = 0.10  # user is deferring floor, not asserting intent to hold it


class BackchannelClassifier:
    """Classifies short user vocalizations as backchannels.

    On each frame: calls the injected BackchannelModel, logs the classification
    event (invariant #1), and returns a TurnSignal populated with p_backchannel.
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
        """Feed one audio frame through the backchannel model (every frame).

        Logs a backchannel_classification event (invariant #1) and returns a
        TurnSignal with p_backchannel from the model and independent p_done /
        p_continue constants (see module docstring for rationale).
        """
        p_backchannel = self._model(frame)
        frame_evt = self._emit("backchannel_classification", caused_by, p_backchannel)
        return TurnSignal(
            detector="backchannel",
            p_done=_P_DONE,
            p_continue=_P_CONTINUE,
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
