"""VisionSidecar stub — unit tests (v0.1c Task 3).

Success criterion (verbatim):
  vision_sidecar.py imports cleanly; ring buffer evicts correctly using event
  timestamps (not wall-clock); the scorer is injectable; under no_camera_memory
  mode the ring buffer is empty and non-resolvable.
"""

import pytest

from companion_harness.vision_sidecar import (
    FrameRef,
    GroundingModel,
    GroundingResult,
    SceneScorer,
    VisionSidecar,
)


# ---------------------------------------------------------------------------
# Fakes

class _FakeSceneScorer:
    """Returns scripted scene-change scores."""

    def __init__(self, scores: list[float]) -> None:
        self._scores = iter(scores)

    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return next(self._scores)


class _FakeGroundingModel:
    """Returns a fixed (label, confidence) pair."""

    def __init__(self, label: str, confidence: float) -> None:
        self._label = label
        self._confidence = confidence

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return self._label, self._confidence


# ---------------------------------------------------------------------------
# Protocol satisfaction

def test_fake_scene_scorer_satisfies_protocol():
    assert isinstance(_FakeSceneScorer([0.5]), SceneScorer)


def test_fake_grounding_model_satisfies_protocol():
    assert isinstance(_FakeGroundingModel("mug", 0.9), GroundingModel)


# ---------------------------------------------------------------------------
# Construction

def test_vision_sidecar_construction():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([]),
        grounding_model=_FakeGroundingModel("", 0.0),
    )
    assert isinstance(sidecar, VisionSidecar)
    assert sidecar.buffer_size() == 0


# ---------------------------------------------------------------------------
# Ring-buffer eviction (keyed on event timestamp_mono_ms, not wall clock)

def test_ring_buffer_stores_frames_within_window():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([0.1, 0.2]),
        grounding_model=_FakeGroundingModel("", 0.0),
        window_ms=60_000,
    )
    ref_a = FrameRef(event_id="ev-a", timestamp_mono_ms=1_000, frame_bytes=b"\x01")
    ref_b = FrameRef(event_id="ev-b", timestamp_mono_ms=30_000, frame_bytes=b"\x02")

    sidecar.ingest_frame(ref_a)
    sidecar.ingest_frame(ref_b)

    assert sidecar.buffer_size() == 2


def test_ring_buffer_evicts_old_frames_by_event_timestamp():
    """Frames whose timestamp_mono_ms is > window_ms behind the latest are evicted."""
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([0.1, 0.2, 0.3]),
        grounding_model=_FakeGroundingModel("", 0.0),
        window_ms=60_000,
    )
    # t=0ms: in window relative to any frame up to t=60_000ms
    ref_a = FrameRef(event_id="ev-a", timestamp_mono_ms=0, frame_bytes=b"\x01")
    # t=30_000ms: in window
    ref_b = FrameRef(event_id="ev-b", timestamp_mono_ms=30_000, frame_bytes=b"\x02")
    # t=61_000ms: ref_a (t=0) is exactly at the cutoff (0 <= 61000-60000=1000); evicted
    ref_c = FrameRef(event_id="ev-c", timestamp_mono_ms=61_000, frame_bytes=b"\x03")

    sidecar.ingest_frame(ref_a)
    sidecar.ingest_frame(ref_b)
    sidecar.ingest_frame(ref_c)

    ids = [r.event_id for r in sidecar.buffer_snapshot()]
    assert "ev-a" not in ids, "frame at t=0 must be evicted when latest frame is at t=61_000"
    assert "ev-b" in ids
    assert "ev-c" in ids


def test_ring_buffer_out_of_window_frame_not_resolvable():
    """A referent outside the ring-buffer window is not resolvable (negative path)."""
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([0.1, 0.2]),
        grounding_model=_FakeGroundingModel("mug", 0.95),
        window_ms=10_000,
    )
    ref_old = FrameRef(event_id="ev-old", timestamp_mono_ms=0, frame_bytes=b"\x01")
    ref_new = FrameRef(event_id="ev-new", timestamp_mono_ms=15_000, frame_bytes=b"\x02")

    sidecar.ingest_frame(ref_old)
    sidecar.ingest_frame(ref_new)  # evicts ref_old (0 <= 15000-10000=5000)

    ids = [r.event_id for r in sidecar.buffer_snapshot()]
    assert "ev-old" not in ids


# ---------------------------------------------------------------------------
# Scene-change scorer is injectable and returns scripted scores

def test_scene_scorer_injectable():
    scorer = _FakeSceneScorer([0.7])
    sidecar = VisionSidecar(
        scene_scorer=scorer,
        grounding_model=_FakeGroundingModel("", 0.0),
    )
    # First frame: no previous frame, score is 0.0
    ref_a = FrameRef(event_id="ev-a", timestamp_mono_ms=1_000, frame_bytes=b"\x01")
    score_a = sidecar.ingest_frame(ref_a)
    assert score_a == 0.0

    # Second frame: scorer is called with scripted value 0.7
    ref_b = FrameRef(event_id="ev-b", timestamp_mono_ms=2_000, frame_bytes=b"\x02")
    score_b = sidecar.ingest_frame(ref_b)
    assert score_b == pytest.approx(0.7)


# ---------------------------------------------------------------------------
# Grounding pass gated by deictic_reference

def test_resolve_without_deictic_reference_returns_not_resolvable():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([]),
        grounding_model=_FakeGroundingModel("mug", 0.9),
    )
    ref = FrameRef(event_id="ev-a", timestamp_mono_ms=1_000, frame_bytes=b"\x01")
    sidecar.ingest_frame(ref)

    result = sidecar.resolve("what is this?", deictic_reference=False)
    assert result.frame_event_id is None


def test_resolve_with_deictic_reference_returns_result():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([]),
        grounding_model=_FakeGroundingModel("mug", 0.9),
    )
    ref = FrameRef(event_id="ev-a", timestamp_mono_ms=1_000, frame_bytes=b"\x01")
    sidecar.ingest_frame(ref)

    result = sidecar.resolve("what is this?", deictic_reference=True)
    assert result.frame_event_id == "ev-a"
    assert result.label == "mug"
    assert result.confidence == pytest.approx(0.9)


def test_resolve_with_empty_buffer_returns_not_resolvable():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([]),
        grounding_model=_FakeGroundingModel("mug", 0.9),
    )
    result = sidecar.resolve("what is this?", deictic_reference=True)
    assert result.frame_event_id is None


# ---------------------------------------------------------------------------
# Privacy guard: no_camera_memory mode

def test_no_camera_memory_buffer_stays_empty():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([]),
        grounding_model=_FakeGroundingModel("mug", 0.9),
        privacy_mode="no_camera_memory",
    )
    ref = FrameRef(event_id="ev-a", timestamp_mono_ms=1_000, frame_bytes=b"\x01")
    sidecar.ingest_frame(ref)

    assert sidecar.buffer_size() == 0


def test_no_camera_memory_resolve_returns_not_resolvable():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([]),
        grounding_model=_FakeGroundingModel("mug", 0.9),
        privacy_mode="no_camera_memory",
    )
    result = sidecar.resolve("what is this?", deictic_reference=True)
    assert result.frame_event_id is None


def test_no_camera_memory_ingest_returns_zero_score():
    sidecar = VisionSidecar(
        scene_scorer=_FakeSceneScorer([0.8]),
        grounding_model=_FakeGroundingModel("", 0.0),
        privacy_mode="no_camera_memory",
    )
    ref = FrameRef(event_id="ev-a", timestamp_mono_ms=1_000, frame_bytes=b"\x01")
    score = sidecar.ingest_frame(ref)
    assert score == 0.0
