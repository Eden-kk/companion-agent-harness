"""Stage 2 — current frame grounding contract test (v0.1c Task 9).

Success criterion (verbatim):
  pytest -k current_frame_grounding passes non-vacuously — the grounding event
  traces to the correct frame, AND a negative-path assertion (a no-deictic-reference
  frame -> NO grounding pass runs).

Fixture: current_frame_grounding_001/case.json
  - Deictic sub-case: deictic_p=0.91 on frames 000-002; gate fires at frame-002
    (target_grounding_frame=True); grounding label=mug confidence=0.92.
  - Non-deictic sub-case: deictic_p=0.10 on frames 100-101; gate does NOT fire;
    no deictic_grounding event emitted.

The test gates the *harness mechanism* only — whether grounding ran against the
correct raw_video_frame and the causal chain closes. Answer content correctness
is the advisory eval layer (b200), not this gate.
"""

import pytest

from companion_harness.deictic_detector import DeicticDetector, DeicticModel
from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.schemas import Event
from companion_harness.vision_sidecar import FrameRef, GroundingModel, SceneScorer, VisionSidecar


# ---------------------------------------------------------------------------
# Fakes

class _ScriptedDeicticModel:
    """Returns (True, p) when p >= threshold, else (False, p). Probability driven by fixture."""

    THRESHOLD = 0.8

    def __init__(self, probabilities: list[float]) -> None:
        self._probs = iter(probabilities)

    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]:
        p = next(self._probs)
        return p >= self.THRESHOLD, p


assert isinstance(_ScriptedDeicticModel([]), DeicticModel)


class _ScriptedGroundingModel:
    """Returns scripted (label, confidence) pairs driven by fixture."""

    def __init__(self, results: list[tuple[str, float]]) -> None:
        self._results = iter(results)

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return next(self._results)


assert isinstance(_ScriptedGroundingModel([]), GroundingModel)


class _FlatSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.0


assert isinstance(_FlatSceneScorer(), SceneScorer)


# ---------------------------------------------------------------------------
# Logger helper

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


# ---------------------------------------------------------------------------
# Test

@pytest.mark.asyncio
async def test_current_frame_grounding() -> None:
    """current_frame_grounding_001: deictic gate fires -> grounding event traces to correct frame.

    Non-vacuity guarantees:
      (a) Deictic sub-case: exactly one deictic_grounding event is emitted.
          Fails if grounding never ran or ran multiple times.
      (b) Grounding event caused_by[] references the target raw_video_frame event_id
          (frame-002, the frame marked target_grounding_frame=True in the fixture).
          Fails if the causal chain is broken or points to the wrong frame.
      (c) Grounding result label='mug' and confidence=0.92 (fixture scripted values).
          Fails if stub injection is ignored.
      (d) Negative path: non-deictic sub-case emits zero deictic_grounding events.
          Fails if the gate does not block grounding when deictic_reference=False.
    """
    fixture = load_fixture("current_frame_grounding_001")
    assert fixture["case_id"] == "current_frame_grounding_001"
    signal_trace = fixture["signal_trace"]

    deictic_frames = [f for f in signal_trace if f["sub_case"] == "deictic"]
    non_deictic_frames = [f for f in signal_trace if f["sub_case"] == "non_deictic"]

    # Identify the target grounding frame (frame-002).
    target_frame = next(f for f in deictic_frames if f.get("target_grounding_frame"))
    assert target_frame["frame_id"] == "frame-002"
    assert target_frame["grounding_result"] == "mug"
    assert target_frame["grounding_confidence"] == pytest.approx(0.92)

    # ------------------------------------------------------------------
    # Build persistent detector/sidecar instances (shared across frames, per task spec).
    logger, collected = _make_logger()
    await logger.start()

    # Scripted grounding model: returns the scripted result for the gate frame.
    grounding_model = _ScriptedGroundingModel([
        (target_frame["grounding_result"], target_frame["grounding_confidence"])
    ])

    sidecar = VisionSidecar(
        scene_scorer=_FlatSceneScorer(),
        grounding_model=grounding_model,
        session_id="cfgtest",
        logger=logger,
    )
    deictic_detector = DeicticDetector(
        model=_ScriptedDeicticModel([f["deictic_p"] for f in deictic_frames]),
        session_id="cfgtest",
        logger=logger,
    )

    # ------------------------------------------------------------------
    # Replay deictic sub-case: ingest frames, classify, resolve.
    target_raw_video_event_id: str | None = None

    for frame in deictic_frames:
        frame_bytes = bytes.fromhex(frame["frame_bytes_hex"])
        frame_event_id = f"cfgtest-raw-{frame['frame_id']}"
        ref = FrameRef(
            event_id=frame_event_id,
            timestamp_mono_ms=frame["timestamp_mono_ms"],
            frame_bytes=frame_bytes,
        )
        sidecar.ingest_frame(ref)

        if frame.get("target_grounding_frame"):
            target_raw_video_event_id = frame_event_id
            deictic_result = deictic_detector.classify(
                transcript="what is this?",
                caused_by=[frame_event_id],
            )
            assert deictic_result.is_deictic, f"deictic gate must fire for deictic_p={frame['deictic_p']}"
            grounding_result = sidecar.resolve(
                "what is this?",
                deictic_reference=deictic_result.is_deictic,
                deictic_evt_id=deictic_result.event_id,
            )

    await logger.stop()

    # (a) Exactly one deictic_grounding event.
    grounding_events = [e for e in collected if e.event_type == "deictic_grounding"]
    assert len(grounding_events) == 1, (
        f"expected 1 deictic_grounding event, got {len(grounding_events)}"
    )

    # (b) Causal chain: grounding event references the correct raw_video_frame.
    ge = grounding_events[0]
    assert target_raw_video_event_id is not None
    assert target_raw_video_event_id in ge.caused_by, (
        f"grounding event caused_by={ge.caused_by!r} does not reference "
        f"target frame event_id={target_raw_video_event_id!r}"
    )

    # (c) Grounding result carries fixture scripted values.
    assert grounding_result.label == "mug", (
        f"grounding label={grounding_result.label!r}, expected 'mug'"
    )
    assert grounding_result.confidence == pytest.approx(0.92), (
        f"grounding confidence={grounding_result.confidence}, expected 0.92"
    )
    assert grounding_result.frame_event_id == target_raw_video_event_id

    # ------------------------------------------------------------------
    # (d) Negative path: non-deictic sub-case must emit NO grounding events.
    nd_logger, nd_collected = _make_logger()
    await nd_logger.start()

    nd_sidecar = VisionSidecar(
        scene_scorer=_FlatSceneScorer(),
        grounding_model=_ScriptedGroundingModel([]),
        session_id="cfgtest-nd",
        logger=nd_logger,
    )
    nd_deictic_detector = DeicticDetector(
        model=_ScriptedDeicticModel([f["deictic_p"] for f in non_deictic_frames]),
        session_id="cfgtest-nd",
        logger=nd_logger,
    )

    classification_frame = next(
        f for f in non_deictic_frames if f["event_type"] == "deictic_classification"
    )
    raw_frames = [f for f in non_deictic_frames if f["event_type"] == "raw_video_frame"]

    for frame in raw_frames:
        frame_bytes = bytes.fromhex(frame["frame_bytes_hex"])
        ref = FrameRef(
            event_id=f"cfgtest-nd-raw-{frame['frame_id']}",
            timestamp_mono_ms=frame["timestamp_mono_ms"],
            frame_bytes=frame_bytes,
        )
        nd_sidecar.ingest_frame(ref)

    nd_frame_event_id = f"cfgtest-nd-raw-{raw_frames[-1]['frame_id']}"
    nd_deictic_result = nd_deictic_detector.classify(
        transcript="what time is it?",
        caused_by=[nd_frame_event_id],
    )
    assert not nd_deictic_result.is_deictic, (
        f"deictic gate must NOT fire for deictic_p={classification_frame['deictic_p']}"
    )
    nd_result = nd_sidecar.resolve(
        "what time is it?",
        deictic_reference=nd_deictic_result.is_deictic,
    )
    assert nd_result.frame_event_id is None, "non-deictic query must not resolve a frame"

    await nd_logger.stop()

    nd_grounding_events = [e for e in nd_collected if e.event_type == "deictic_grounding"]
    assert len(nd_grounding_events) == 0, (
        f"non-deictic sub-case must emit 0 grounding events, got {len(nd_grounding_events)}"
    )
