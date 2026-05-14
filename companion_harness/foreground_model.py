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

# SPEC AMBIGUITY (Part 3): Part 3 lists both ForegroundModel ("full-duplex
# multimodal speech/text") and ThinkerProposalGen ("proposal-only, no direct
# speech path") as distinct adapters, but gives neither a concrete API shape.
# At the call-site level, both take input and emit ThinkerProposal candidates.
# Part 9 assigns MiniCPM-o 4.5 (as_duplex) to ForegroundModel and the Inner
# Thoughts loop to ThinkerProposalGen — so the distinction is the underlying
# model/mode, not the adapter API.  This module is ForegroundModel (per ROADMAP
# Task 7 and Part 9).  If the spec ever differentiates the two APIs (e.g.,
# ForegroundModel also handles barge-in or TTS), this module must be updated.
#
# Event types: foreground_frame and foreground_proposal extend the Part 5 list
# (which covers AudioOutputController only and is explicitly "not exhaustive").

## Protocol structure (ADR note)

Two Protocols are defined here:

`DuplexModel` — base one-shot interface: `infer(audio_frame, video_frame=None)`.
  The optional `video_frame` parameter is additive; existing audio-only fakes
  still satisfy this Protocol unchanged because `@runtime_checkable` only checks
  method *existence*, not signature.  Audio-only fakes can ignore the parameter.

`StreamingDuplexModel` — extends `DuplexModel` with an async-generator method
  `infer_stream(frame_iter, caused_by)` that yields `ThinkerProposal` candidates
  incrementally from a frame stream.  This is a *separate* Protocol rather than
  a new required method on `DuplexModel`, so that audio-only fakes continue to
  satisfy `isinstance(fake, DuplexModel)`.  Only streaming-capable models
  implement `StreamingDuplexModel`.  Both Protocols are `@runtime_checkable`.

`ForegroundModel` gains `process_stream()` which drives an injected
  `StreamingDuplexModel` and logs `foreground_frame` / `foreground_proposal`
  events for every yielded proposal (invariant #1, every event carries
  `caused_by[]`).
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import AsyncGenerator, AsyncIterator, Protocol, runtime_checkable

from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event, ThinkerProposal

__all__ = ["DuplexModel", "StreamingDuplexModel", "ForegroundModel"]


@runtime_checkable
class DuplexModel(Protocol):
    """Injected interface for a full-duplex multimodal model.

    The real implementation wraps MiniCPM-o 4.5 (as_duplex mode) on b200
    (requires torch + CUDA). Tests inject a fake that returns scripted proposals.
    """

    def infer(
        self, audio_frame: bytes, video_frame: bytes | None = None
    ) -> ThinkerProposal | None: ...


@runtime_checkable
class StreamingDuplexModel(DuplexModel, Protocol):
    """Extension of DuplexModel for models that support streaming (incremental) inference.

    Consuming models call `infer_stream` with an async iterator of (audio, video)
    frame pairs and iterate over the yielded ThinkerProposal candidates.
    Only models that implement this method satisfy `isinstance(m, StreamingDuplexModel)`.
    Audio-only one-shot fakes satisfy `DuplexModel` but NOT `StreamingDuplexModel`.
    """

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
    ) -> AsyncGenerator[ThinkerProposal, None]: ...


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
        self,
        audio_frame: bytes,
        caused_by: list[str],
        video_frame: bytes | None = None,
    ) -> ThinkerProposal | None:
        """Feed one audio (and optional video) frame to the model.

        Logs a foreground_frame event for every frame (invariant #1).
        Returns a ThinkerProposal when the model produces a candidate, else None.
        The returned proposal must pass through SpeakPolicy before becoming speech.
        """
        frame_evt = self._emit("foreground_frame", caused_by, "raw_audio")
        proposal = self._model.infer(audio_frame, video_frame)
        if proposal is None:
            return None
        if not proposal.caused_by:
            proposal.caused_by = [frame_evt.event_id]
        self._emit("foreground_proposal", [frame_evt.event_id], "model_output")
        return proposal

    async def process_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
    ) -> AsyncGenerator[ThinkerProposal, None]:
        """Drive a StreamingDuplexModel and yield logged ThinkerProposal candidates.

        Logs a foreground_frame event before invoking the model, and a
        foreground_proposal event for each yielded proposal (invariant #1).
        The injected model must satisfy StreamingDuplexModel.
        """
        frame_evt = self._emit("foreground_frame", caused_by, "raw_audio")
        async for proposal in await self._model.infer_stream(  # type: ignore[attr-defined]
            frame_iter, [frame_evt.event_id]
        ):
            if not proposal.caused_by:
                proposal.caused_by = [frame_evt.event_id]
            self._emit("foreground_proposal", [frame_evt.event_id], "model_output")
            yield proposal

    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit(self, event_type: str, caused_by: list[str], payload_kind: str = "model_output") -> Event:
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
            payload_kind=payload_kind,  # type: ignore[arg-type]
            subject_class="self",
            sensitivity="safe",
            retention_policy_id="default",
        )
        self._logger.log(evt)
        return evt
