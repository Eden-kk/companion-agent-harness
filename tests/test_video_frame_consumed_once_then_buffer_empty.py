"""Consume-once semantics: a buffered frame fires once, then is cleared.

Plan: docs/plan-vision-sidecar-wiring.md Anchor 2.

Success criterion: after ingest_frame_bytes() the next consume_pending_frame()
returns the frame; the subsequent consume_pending_frame() returns None (the
buffer is single-slot most-recent-frame, not a repeating broadcast).
"""

from __future__ import annotations

from companion_harness.vision_sidecar import VisionSidecar


class _NullSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0


class _NullGroundingModel:
    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return "", 0.0


def test_consume_once_then_buffer_empty() -> None:
    sidecar = VisionSidecar(
        scene_scorer=_NullSceneScorer(),
        grounding_model=_NullGroundingModel(),
        session_id="test-session",
    )
    sidecar.ingest_frame_bytes(b"jpeg", event_id="raw-video-1", timestamp_mono_ms=100)

    first = sidecar.consume_pending_frame()
    assert first is not None
    assert first[0] == b"jpeg"

    second = sidecar.consume_pending_frame()
    assert second is None
