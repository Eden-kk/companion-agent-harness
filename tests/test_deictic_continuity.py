"""Deictic continuity contract test — v0.1c Task 10.

Success criterion (verbatim):
  pytest -k deictic_continuity passes non-vacuously — the resolved referent
  traces (via caused_by) to the EARLIER raw_video_frame event, NOT the
  current one.

Fixture: companion_harness/fixtures/deictic_continuity_001/case.json

Scenario: two utterances.
  First utterance: "what is this?" — deictic gate fires; grounding resolves
    to the most-recent buffer frame (frame-002).
  Second utterance: "what about that one?" — deictic gate fires again but
    the referent is the PRIOR grounded frame (frame-002, mug), NOT the
    current frame (frame-100/101, book).

The sidecar and detector instances are shared across both utterances
(persistent across frames, as specified).
"""

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.schemas import Event
from companion_harness.vision_sidecar import FrameRef, VisionSidecar


# ---------------------------------------------------------------------------
# Scripted fakes


class _ScriptedSceneScorer:
    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores  # frame_id -> score

    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        # key on curr_frame identity byte (index byte at position 5)
        idx = curr_frame[5] if len(curr_frame) > 5 else 0
        frame_id = f"frame-{idx:03d}"
        return self._scores.get(frame_id, 0.0)


class _ScriptedGroundingModel:
    def __init__(self, results: dict[bytes, tuple[str, float]]) -> None:
        self._results = results  # frame_bytes -> (label, confidence)

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return self._results.get(frame, ("", 0.0))


# ---------------------------------------------------------------------------
# Helpers


def _make_frame_ref(frame_id: str, timestamp_mono_ms: int, frame_bytes_hex: str) -> FrameRef:
    return FrameRef(
        event_id=frame_id,
        timestamp_mono_ms=timestamp_mono_ms,
        frame_bytes=bytes.fromhex(frame_bytes_hex),
    )


# ---------------------------------------------------------------------------
# Contract test


@pytest.mark.asyncio
async def test_deictic_continuity_prior_referent() -> None:
    """Second grounding caused_by must reference frame-002 (prior referent), not frame-100.

    This is the success criterion for v0.1c Task 10.
    """
    fixture = load_fixture("deictic_continuity_001")
    trace = fixture["signal_trace"]

    # Index trace entries by frame_id for convenient lookup
    by_frame = {entry["frame_id"]: entry for entry in trace}

    # Build scripted grounding model: maps frame_bytes to (label, confidence)
    grounding_map: dict[bytes, tuple[str, float]] = {}
    for entry in trace:
        fb = bytes.fromhex(entry["frame_bytes_hex"])
        grounding_map[fb] = (entry["grounding_result"], entry["grounding_confidence"])

    # Build scripted scene scorer: maps frame_id to scene_change_score
    score_map: dict[str, float] = {e["frame_id"]: e["scene_change_score"] for e in trace}

    collected: list[Event] = []

    async def sink(event: Event) -> None:
        collected.append(event)

    logger = EventLogger(sink)
    await logger.start()

    sidecar = VisionSidecar(
        scene_scorer=_ScriptedSceneScorer(score_map),
        grounding_model=_ScriptedGroundingModel(grounding_map),
        session_id="deictic-continuity-001",
        logger=logger,
    )

    # -----------------------------------------------------------------------
    # First utterance: "what is this?" — frames 000, 001, 002
    # Ingest frames and run grounding on frame-002 (most recent at EOU)

    first_utterance_frames = ["frame-000", "frame-001", "frame-002"]
    for fid in first_utterance_frames:
        entry = by_frame[fid]
        ref = _make_frame_ref(fid, entry["timestamp_mono_ms"], entry["frame_bytes_hex"])
        sidecar.ingest_frame(ref)

    first_deictic_evt_id = "deictic-classification-first"
    first_result = sidecar.resolve(
        "what is this?",
        deictic_reference=True,
        deictic_evt_id=first_deictic_evt_id,
    )

    # First grounding resolves to the most recent buffered frame (frame-002)
    assert first_result.frame_event_id == "frame-002", (
        f"First grounding expected frame-002, got {first_result.frame_event_id!r}"
    )
    assert first_result.label == "mug"

    prior_referent_frame_id = first_result.frame_event_id  # "frame-002"

    # -----------------------------------------------------------------------
    # Second utterance: "what about that one?" — frames 100, 101, 102
    # Current frame changes (book visible), but referent is the PRIOR (mug, frame-002)

    second_utterance_frames = ["frame-100", "frame-101", "frame-102"]
    for fid in second_utterance_frames:
        entry = by_frame[fid]
        ref = _make_frame_ref(fid, entry["timestamp_mono_ms"], entry["frame_bytes_hex"])
        sidecar.ingest_frame(ref)

    second_deictic_evt_id = "deictic-classification-second"
    second_result = sidecar.resolve(
        "what about that one?",
        deictic_reference=True,
        deictic_evt_id=second_deictic_evt_id,
        prior_referent_frame_id=prior_referent_frame_id,
    )

    # Second grounding must resolve to frame-002 (mug), NOT the current frame (frame-101/book)
    assert second_result.frame_event_id == "frame-002", (
        f"Second grounding expected prior referent frame-002, got {second_result.frame_event_id!r}"
    )
    assert second_result.label == "mug", (
        f"Second grounding expected label='mug' (prior referent), got {second_result.label!r}"
    )

    await logger.stop()

    # -----------------------------------------------------------------------
    # Causal chain assertion: the SECOND grounding event's caused_by must
    # reference frame-002, NOT frame-100 or frame-101.

    grounding_events = [e for e in collected if e.event_type == "deictic_grounding"]
    assert len(grounding_events) == 2, (
        f"Expected 2 grounding events (one per utterance), got {len(grounding_events)}"
    )

    second_grounding = grounding_events[1]
    assert "frame-002" in second_grounding.caused_by, (
        f"Second grounding caused_by={second_grounding.caused_by!r} "
        f"must reference prior referent 'frame-002', not current frame"
    )
    assert "frame-100" not in second_grounding.caused_by, (
        f"Second grounding caused_by={second_grounding.caused_by!r} "
        f"must NOT reference the current frame 'frame-100'"
    )
    assert "frame-101" not in second_grounding.caused_by, (
        f"Second grounding caused_by={second_grounding.caused_by!r} "
        f"must NOT reference the current frame 'frame-101'"
    )
    assert second_deictic_evt_id in second_grounding.caused_by, (
        f"Second grounding caused_by must include deictic classification event"
    )
