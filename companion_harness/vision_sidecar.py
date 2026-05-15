"""VisionSidecar — Stage 2 visual-memory ring buffer and scene-change scoring stub (v0.1c Task 3).

See docs/architecture-v0.1.md §Part 3 (VisionSidecar: frames + scene change + deictic),
§Part 6 Stage 2 (audio-video grounding), and §Part 7 (privacy-mode ↔ adapter compatibility).

## Design notes

Ring-buffer eviction is keyed off `timestamp_mono_ms` from `raw_video_frame` events, NOT
wall-clock time.monotonic(). This makes the 60s window bound deterministic and fixture-testable:
a test can inject timestamps freely without waiting real time.

Under `privacy_mode == "no_camera_memory"` (Part 7) the ring buffer is disabled: no frame
references are stored and any resolution attempt returns not-resolvable. This is enforced at the
stub level — not deferred to Stage 4.

The scene-change scorer and grounding model are injected via the `SceneScorer` and
`GroundingModel` Protocols so the core module never imports a model SDK. Real implementations
live on b200.

The frame-grounding pass is gated by `PolicyInputs.deictic_reference`: when False the pass
does not run. Full wiring of the grounding pass is implemented in v0.1c Task 7.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from companion_harness.av_conflict_scorer import AudioVisualConflictScorer, _NullAudioVisualConflictScorer
from companion_harness.event_logger import EventLogger
from companion_harness.schemas import Event

__all__ = ["SceneScorer", "GroundingModel", "_NullGroundingModel", "FrameRef", "GroundingResult", "VisionSidecar", "_NullSceneScorer"]

_WINDOW_MS: int = 60_000  # 60-second ring-buffer window


@runtime_checkable
class SceneScorer(Protocol):
    """Injected interface for scene-change scoring.

    Receives consecutive frame blobs; returns a scene-change score in [0.0, 1.0].
    Tests inject a fake that returns scripted scores.
    """

    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float: ...


class _NullSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0  # UNAVAILABLE: #166 — real CLIP cosine scorer pending model wiring


@runtime_checkable
class GroundingModel(Protocol):
    """Injected interface for deictic-grounding inference.

    Receives a frame blob and a query string; returns a (label, confidence) pair.
    Tests inject a fake that returns scripted results.
    The real implementation loads MiniCPM-o on b200 with init_vision=True.
    """

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]: ...


class _NullGroundingModel:
    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return "", 0.0  # UNAVAILABLE: #161 — real grounding model pending model wiring


@dataclass
class FrameRef:
    """A lightweight reference to a raw_video_frame event held in the ring buffer."""

    event_id: str
    timestamp_mono_ms: int
    frame_bytes: bytes


@dataclass
class GroundingResult:
    label: str
    confidence: float
    frame_event_id: str | None  # None when not-resolvable


_NOT_RESOLVABLE = GroundingResult(label="", confidence=0.0, frame_event_id=None)


class VisionSidecar:
    """Stage 2 visual-memory ring buffer, scene-change scoring, and deictic grounding stub.

    Ring buffer holds at most the last `window_ms` milliseconds of FrameRefs,
    evicted by event timestamp (not wall clock). Under `no_camera_memory` the
    buffer is permanently disabled.

    Scene-change scoring and deictic grounding are injected via the Protocols above.
    The grounding pass only runs when `deictic_reference=True` (Part 6 gate).
    Full grounding wiring is Task 7; this stub establishes the skeleton and
    the ring-buffer + privacy-guard invariants.
    """

    SOURCE = "vision_sidecar"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        scene_scorer: SceneScorer,
        grounding_model: GroundingModel,
        *,
        av_conflict_scorer: AudioVisualConflictScorer | None = None,
        session_id: str = "",
        logger: EventLogger | None = None,
        privacy_mode: str = "default",
        window_ms: int = _WINDOW_MS,
    ) -> None:
        self._scene_scorer = scene_scorer
        self._grounding_model = grounding_model
        self._av_conflict_scorer: AudioVisualConflictScorer = (
            av_conflict_scorer if av_conflict_scorer is not None else _NullAudioVisualConflictScorer()
        )
        self._session_id = session_id
        self._logger = logger
        self._privacy_mode = privacy_mode
        self._window_ms = window_ms
        self._buffer: deque[FrameRef] = deque()
        self._last_frame: bytes | None = None
        self._last_scene_change_score: float = 0.0
        self._last_grounding_confidence: float = 0.0
        self._seq = 0
        # Live-loop pairing buffer: single most-recent frame, consumed by
        # StreamingRealtimeOrchestrator._bounded_frame_gen on the next audio
        # chunk. Independent of the ring buffer above (which serves Stage 2
        # deictic grounding). See plan-vision-sidecar-wiring.md Anchor 2.
        self._pending_frame: tuple[bytes, str] | None = None
        self._last_frame_event_id: str | None = None

    # ------------------------------------------------------------------
    # Frame ingest

    def ingest_frame(self, ref: FrameRef) -> float:
        """Add a frame reference to the ring buffer and return the scene-change score.

        Under `no_camera_memory` the frame is not stored and 0.0 is returned.
        Evicts frames whose `timestamp_mono_ms` is more than `window_ms` behind `ref`.
        """
        if self._privacy_mode == "no_camera_memory":
            return 0.0

        score = 0.0
        if self._last_frame is not None:
            score = self._scene_scorer(self._last_frame, ref.frame_bytes)
        self._last_frame = ref.frame_bytes
        self._last_scene_change_score = score

        self._buffer.append(ref)
        self._evict(ref.timestamp_mono_ms)
        return score

    # ------------------------------------------------------------------
    # Live-loop pairing buffer (single most-recent frame for foreground model)

    def ingest_frame_bytes(
        self,
        frame_bytes: bytes,
        event_id: str,
        timestamp_mono_ms: int,
    ) -> None:
        """Buffer a raw video frame for the next foreground audio chunk.

        Logs a `vision_frame` event (caused_by=[event_id]) and stores the
        (frame_bytes, vision_frame.event_id) pair so the next call to
        `consume_pending_frame()` returns it. Under `no_camera_memory`
        the frame is not stored and no event is logged.
        """
        if self._privacy_mode == "no_camera_memory":
            return

        seq = self._next_seq()
        vision_event_id = f"{self._session_id}-vision-{seq}-{timestamp_mono_ms}"
        if self._logger is not None:
            payload_hash = hashlib.sha256(
                f"vision_frame:{vision_event_id}:{event_id}".encode()
            ).hexdigest()[:16]
            evt = Event(
                event_id=vision_event_id,
                session_id=self._session_id,
                schema_version=self.SCHEMA_VERSION,
                seq_no=seq,
                event_type="vision_frame",
                timestamp_mono_ms=timestamp_mono_ms,
                timestamp_wall=datetime.now(timezone.utc).isoformat(),
                source=self.SOURCE,
                caused_by=[event_id],
                payload_hash=payload_hash,
                payload_ref=None,
                payload_kind="raw_video",
                subject_class="self",
                sensitivity="sensitive",
                retention_policy_id="raw_media_default_300s",
            )
            self._logger.log(evt)

        self._pending_frame = (frame_bytes, vision_event_id)
        self._last_frame_event_id = vision_event_id

    def consume_pending_frame(self) -> tuple[bytes, str] | None:
        """Return + clear the pending frame, or None if none / privacy gate active.

        Called by StreamingRealtimeOrchestrator._bounded_frame_gen on every
        audio chunk. Single-frame pairing: a frame fires once and is then
        cleared so MiniCPM-o does not re-run its vision tower on the same
        frame for consecutive audio chunks.
        """
        if self._privacy_mode == "no_camera_memory":
            return None
        frame = self._pending_frame
        self._pending_frame = None
        return frame

    def has_pending_frame(self) -> bool:
        return self._pending_frame is not None

    def frames_buffered(self) -> int:
        """Number of frames currently in the live-loop pairing buffer (0 or 1)."""
        return 1 if self._pending_frame is not None else 0

    def last_frame_event_id(self) -> str | None:
        """Event id of the most recently logged vision_frame, or None."""
        return self._last_frame_event_id

    def last_scene_change_score(self) -> float:
        """Most recently computed scene-change score, or 0.0 if no pair seen yet."""
        return self._last_scene_change_score

    def grounding_confidence(self) -> float:
        """Most recently resolved grounding confidence, or 0.0 if no resolve call yet."""
        return self._last_grounding_confidence

    def on_privacy_mode_change(self, new_mode: str) -> None:
        """Update privacy mode; clear pending frame on transition to no_camera_memory."""
        self._privacy_mode = new_mode
        if new_mode == "no_camera_memory":
            self._pending_frame = None

    def audio_visual_conflict_score(self, audio: bytes, frame: bytes | None = None) -> float:
        """Return AV conflict score via the injected scorer (default: _NullAudioVisualConflictScorer)."""
        return self._av_conflict_scorer.score(audio, frame)

    # ------------------------------------------------------------------
    # Grounding pass (gated by deictic_reference)

    def resolve(
        self,
        query: str,
        *,
        deictic_reference: bool,
        deictic_evt_id: str = "",
        prior_referent_frame_id: str = "",
    ) -> GroundingResult:
        """Run the deictic-grounding pass against a buffered frame.

        When `prior_referent_frame_id` is set, resolves against that specific
        frame from the ring buffer (continuity: "that one" → prior referent).
        Otherwise resolves against the most recent buffered frame.

        Returns not-resolvable when:
        - `deictic_reference` is False (gate not open),
        - the ring buffer is empty (no frames in window or no_camera_memory mode),
        - or privacy_mode is no_camera_memory.
        """
        if not deictic_reference:
            return _NOT_RESOLVABLE
        if self._privacy_mode == "no_camera_memory" or not self._buffer:
            return _NOT_RESOLVABLE

        if prior_referent_frame_id:
            frame_ref = next(
                (r for r in self._buffer if r.event_id == prior_referent_frame_id),
                None,
            )
            if frame_ref is None:
                return _NOT_RESOLVABLE
        else:
            frame_ref = self._buffer[-1]
        label, confidence = self._grounding_model(frame_ref.frame_bytes, query)
        self._last_grounding_confidence = confidence
        result = GroundingResult(
            label=label,
            confidence=confidence,
            frame_event_id=frame_ref.event_id,
        )
        self._emit_grounding_event(frame_ref, label, confidence, deictic_evt_id)
        return result

    # ------------------------------------------------------------------
    # Buffer introspection

    def buffer_size(self) -> int:
        return len(self._buffer)

    def buffer_snapshot(self) -> list[FrameRef]:
        return list(self._buffer)

    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _emit_grounding_event(self, frame_ref: FrameRef, label: str, confidence: float, deictic_evt_id: str) -> None:
        if self._logger is None:
            return
        seq = self._next_seq()
        ts_ms = frame_ref.timestamp_mono_ms
        event_id = f"{self._session_id}-grounding-{seq}-{ts_ms}"
        payload_hash = hashlib.sha256(
            f"deictic_grounding:{event_id}:{label}:{confidence:.4f}".encode()
        ).hexdigest()[:16]
        caused_by = [frame_ref.event_id, deictic_evt_id] if deictic_evt_id else [frame_ref.event_id]
        evt = Event(
            event_id=event_id,
            session_id=self._session_id,
            schema_version=self.SCHEMA_VERSION,
            seq_no=seq,
            event_type="deictic_grounding",
            timestamp_mono_ms=ts_ms,
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

    def _evict(self, current_ts_ms: int) -> None:
        cutoff = current_ts_ms - self._window_ms
        while self._buffer and self._buffer[0].timestamp_mono_ms <= cutoff:
            self._buffer.popleft()
