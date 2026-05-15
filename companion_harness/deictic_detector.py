"""DeicticDetector — deictic-reference classifier stub (v0.1c Task 5).

## ADR note: relationship to TurnDetectorSuite

`DeicticDetector` is a **separate adapter**, not a member of `TurnDetectorSuite`.
`TurnDetectorSuite` fans audio frames to EOU-signal detectors (VAD, SmartTurn,
Backchannel) and aggregates turn-completion signals. `DeicticDetector` operates
on a different input (the current-turn transcript or audio buffer after EOU) and
answers a different question: does this utterance contain a deictic reference
("this", "that one", "what did I just pick up")?  Its output — a boolean
`deictic_reference` — populates `PolicyInputs.deictic_reference` and gates the
`VisionSidecar` grounding pass.  Keeping it separate preserves independent
ablation: the deictic gate can be swapped or disabled without touching EOU
detection.

The real classifier is injected via the `DeicticModel` Protocol.  This module
never imports torch, onnxruntime, or any model SDK.  Tests inject a fake
that returns scripted results.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from companion_harness.event_logger import EventLogger

__all__ = ["DeicticModel", "DeicticDetector"]


@runtime_checkable
class DeicticModel(Protocol):
    """Injected interface for a deictic-reference classifier.

    The real implementation wraps a classifier (v0.1c Task 6).
    Tests inject a fake that returns scripted (is_deictic, confidence) pairs.

    Receives the current-turn transcript text and optionally the raw audio
    buffer; returns a (is_deictic, confidence) tuple.
    """

    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]: ...


class DeicticDetector:
    """Classifies whether the current utterance contains a deictic reference.

    Invokes the injected DeicticModel on the turn transcript (and optionally
    the audio buffer), logs every invocation as an event (invariant #1), and
    returns the (is_deictic, confidence) result that populates
    PolicyInputs.deictic_reference.
    """

    SOURCE = "deictic_detector"
    SCHEMA_VERSION = "0.1"

    def __init__(
        self,
        model: DeicticModel,
        session_id: str,
        logger: EventLogger,
    ) -> None:
        self._model = model
        self._session_id = session_id
        self._logger = logger

    def classify(
        self,
        transcript: str,
        caused_by: list[str],
        audio_buffer: bytes | None = None,
    ) -> tuple[bool, float]:
        """Classify a completed utterance for deictic references.

        Task 6 implements this: calls self._model, logs a deictic_classification
        event (invariant #1), and returns (is_deictic, confidence).
        """
        raise NotImplementedError
