"""VisionSidecar live-loop pairing buffer — buffer holds last write.

Plan: docs/plan-vision-sidecar-wiring.md Anchor 2 + Anchor 4.

Success criterion: ingest_frame_bytes() called N times leaves the buffer holding
the most-recent frame; consume_pending_frame() returns that frame + clears the
buffer; older frames are NOT preserved (single-slot most-recent-frame buffer).
"""

from __future__ import annotations

from companion_harness.vision_sidecar import VisionSidecar


class _NullSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0


class _NullGroundingModel:
    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return "", 0.0


def _make_sidecar() -> VisionSidecar:
    return VisionSidecar(
        scene_scorer=_NullSceneScorer(),
        grounding_model=_NullGroundingModel(),
        session_id="test-session",
    )


def test_buffer_holds_last_write() -> None:
    sidecar = _make_sidecar()
    sidecar.ingest_frame_bytes(b"frame-1", event_id="raw-video-1", timestamp_mono_ms=100)
    sidecar.ingest_frame_bytes(b"frame-2", event_id="raw-video-2", timestamp_mono_ms=200)
    sidecar.ingest_frame_bytes(b"frame-3", event_id="raw-video-3", timestamp_mono_ms=300)

    assert sidecar.has_pending_frame() is True
    assert sidecar.frames_buffered() == 1

    pair = sidecar.consume_pending_frame()
    assert pair is not None
    frame_bytes, vision_event_id = pair
    assert frame_bytes == b"frame-3"
    assert "vision" in vision_event_id


def test_consume_clears_buffer() -> None:
    sidecar = _make_sidecar()
    sidecar.ingest_frame_bytes(b"only", event_id="raw-video-1", timestamp_mono_ms=100)
    assert sidecar.frames_buffered() == 1

    pair = sidecar.consume_pending_frame()
    assert pair is not None
    assert pair[0] == b"only"

    assert sidecar.frames_buffered() == 0
    assert sidecar.has_pending_frame() is False


def test_no_camera_memory_blocks_ingest() -> None:
    sidecar = VisionSidecar(
        scene_scorer=_NullSceneScorer(),
        grounding_model=_NullGroundingModel(),
        session_id="test-session",
        privacy_mode="no_camera_memory",
    )
    sidecar.ingest_frame_bytes(b"blocked", event_id="raw-video-1", timestamp_mono_ms=100)
    assert sidecar.frames_buffered() == 0
    assert sidecar.consume_pending_frame() is None


def test_privacy_mode_change_clears_buffer() -> None:
    sidecar = _make_sidecar()
    sidecar.ingest_frame_bytes(b"frame", event_id="raw-video-1", timestamp_mono_ms=100)
    assert sidecar.frames_buffered() == 1

    sidecar.on_privacy_mode_change("no_camera_memory")
    assert sidecar.frames_buffered() == 0
    assert sidecar.consume_pending_frame() is None
