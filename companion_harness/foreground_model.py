"""ForegroundModel adapter — one interface, one concrete instantiation.

See docs/architecture-v0.1.md §Part 3 (ForegroundModel adapter) and §Part 8
(v0.1a enables one ForegroundModel instantiation: MiniCPM-o 4.5 as_duplex).

Invariant #2 (no direct Thinker speech): the model emits ThinkerProposal
candidates only. The policy layer (speak_policy.py) decides whether any
candidate becomes speech.

Invariant #4 (no proactive speech without policy approval): ForegroundModel
never calls into SpeakPolicy and never produces a SpeakDecision.

The real model (MiniCPM-o 4.5) requires torch and lives on b200.
This module never imports torch — the model is injected via the DuplexModel
Protocol so the adapter is locally importable without GPU dependencies.
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, ThinkerProposal

__all__ = ["DuplexModel", "ForegroundModel"]


@runtime_checkable
class DuplexModel(Protocol):
    """Injected interface for a full-duplex multimodal model.

    The real implementation wraps MiniCPM-o 4.5 (as_duplex mode) on b200
    (requires torch + CUDA). Tests inject a fake that returns scripted proposals.
    """

    def infer(self, audio_frame: bytes) -> ThinkerProposal | None: ...


class ForegroundModel:
    """Adapter around an injected DuplexModel.

    Feeds audio frames to the model. When the model produces a candidate,
    wraps it in a logged event and returns it as a ThinkerProposal.
    The proposal is NOT speech — the policy layer decides whether it is spoken.
    """

    SOURCE = "foreground_model"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        model: DuplexModel,
        session_id: str,
        logger: EventLogger,
    ) -> None:
        self._model = model
        self._session_id = session_id
        self._logger = logger
        self._seq = 0

    def process_frame(
        self, audio_frame: bytes, caused_by: list[str]
    ) -> ThinkerProposal | None:
        """Feed one audio frame to the model.

        Logs a foreground_frame event for every frame (invariant #1).
        Returns a ThinkerProposal when the model produces a candidate, else None.
        The returned proposal must pass through SpeakPolicy before becoming speech.
        """
        frame_evt = self._emit("foreground_frame", caused_by)
        proposal = self._model.infer(audio_frame)
        if proposal is None:
            return None
        self._emit("foreground_proposal", [frame_evt.event_id])
        return proposal

    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, event_type: str, caused_by: list[str]) -> Event:
        now_ms = int(time.monotonic() * 1000)
        seq = self._next_seq()
        event_id = f"{self._session_id}-fm-{seq}-{now_ms}"
        payload_hash = hashlib.sha256(
            f"{event_type}:{event_id}".encode()
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
            payload_kind="model_output",
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="default",
        )
        self._logger.log(evt)
        return evt
