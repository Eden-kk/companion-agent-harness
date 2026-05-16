"""Stage 2 — DeicticDetector ablation: same fixture replayed through enabled and disabled paths.

Each path's logged decisions must be attributable to whether the deictic gate was active.
Distinct from Stage 1's test_detector_ablation (which covers TurnDetectorSuite ablation).

Success criterion (Task 17): pytest -k deictic_detector_ablation passes non-vacuously —
per-path attribution is genuine: the test FAILS if the deictic gate's effect is not
observable in the decision trace (e.g. grounding runs in both paths, or neither path is
distinguishable).
"""

import pytest

from companion_harness.deictic_detector import DeicticDetector, DeicticModel
from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.schemas import Event
from companion_harness.vision_sidecar import FrameRef, GroundingModel, SceneScorer, VisionSidecar


# ---------------------------------------------------------------------------
# Stubs

class _ScriptedDeicticModel:
    """Returns scripted (is_deictic, confidence) from the fixture's deictic_p."""

    def __init__(self, deictic_p: float) -> None:
        self._deictic_p = deictic_p

    def __call__(self, transcript: str, audio_buffer: bytes | None) -> tuple[bool, float]:
        return self._deictic_p >= 0.5, self._deictic_p


assert isinstance(_ScriptedDeicticModel(0.91), DeicticModel)


class _FixedSceneScorer:
    def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
        return 0.05


assert isinstance(_FixedSceneScorer(), SceneScorer)


class _ScriptedGroundingModel:
    """Returns the scripted (grounding_result, grounding_confidence) from the fixture."""

    def __init__(self, label: str, confidence: float) -> None:
        self._label = label
        self._confidence = confidence

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return self._label, self._confidence


assert isinstance(_ScriptedGroundingModel("mug", 0.91), GroundingModel)


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
async def test_deictic_detector_ablation():
    """deictic_detector_ablation_001: same trace replayed through enabled and disabled paths.

    Non-vacuity guarantees:
      (a) Enabled path: exactly one deictic_grounding event is emitted.
          Fails if grounding is never triggered when the detector is active.
      (b) Disabled path: zero deictic_grounding events are emitted.
          Fails if grounding fires even though the deictic gate is off.
      (c) Enabled path deictic_grounding event has caused_by containing the
          deictic_classification event_id AND a frame event_id.
          Fails if the causal chain is broken (invariant #1).
      (d) The two paths are distinguishable: enabled trace != disabled trace.
          Fails if the ablation had no observable effect.
    """
    fixture = load_fixture("deictic_detector_ablation_001")
    assert fixture["case_id"] == "deictic_detector_ablation_001"

    signal_trace = fixture["signal_trace"]
    ablation_paths = fixture["ablation_paths"]

    enabled_cfg = next(p for p in ablation_paths if p["path_id"] == "enabled")
    disabled_cfg = next(p for p in ablation_paths if p["path_id"] == "disabled")

    # Use the gate frame (seq_no=2, deictic_classification role) for the classify call.
    gate_frame = next(f for f in signal_trace if f.get("resolution_frame"))
    deictic_p = gate_frame["deictic_p"]
    grounding_label = gate_frame["grounding_result"]
    grounding_confidence = gate_frame["grounding_confidence"]

    # ------------------------------------------------------------------
    # ENABLED path: DeicticDetector active; grounding pass fires.
    # ------------------------------------------------------------------
    logger_en, events_en = _make_logger()
    await logger_en.start()

    detector_en = DeicticDetector(
        model=_ScriptedDeicticModel(deictic_p),
        session_id="ablation-enabled",
        logger=logger_en,
    )
    sidecar_en = VisionSidecar(
        scene_scorer=_FixedSceneScorer(),
        grounding_model=_ScriptedGroundingModel(grounding_label, grounding_confidence),
        session_id="ablation-enabled",
        logger=logger_en,
    )

    # Ingest video frames from the signal trace.
    frame_events_en: list[str] = []
    for entry in signal_trace:
        frame_bytes = bytes.fromhex(entry["frame_bytes_hex"])
        frame_event_id = f"ablation-enabled-frame-{entry['seq_no']}"
        frame_events_en.append(frame_event_id)
        ref = FrameRef(
            event_id=frame_event_id,
            timestamp_mono_ms=entry["timestamp_mono_ms"],
            frame_bytes=frame_bytes,
        )
        sidecar_en.ingest_frame(ref)

    # Classify the utterance via DeicticDetector (uses gate frame's deictic_p).
    cause_en = [frame_events_en[-1]]
    deictic_en = detector_en.classify("what is this?", caused_by=cause_en)
    assert deictic_en.is_deictic is True, "enabled path: deictic_p=0.91 must yield is_deictic=True"

    # Run grounding pass (gate open).
    result_en = sidecar_en.resolve(
        "what is this?",
        deictic_reference=deictic_en.is_deictic,
        deictic_evt_id=deictic_en.event_id,
    )

    await logger_en.stop()

    # ------------------------------------------------------------------
    # DISABLED path: no DeicticDetector; deictic_reference stays False.
    # ------------------------------------------------------------------
    logger_dis, events_dis = _make_logger()
    await logger_dis.start()

    sidecar_dis = VisionSidecar(
        scene_scorer=_FixedSceneScorer(),
        grounding_model=_ScriptedGroundingModel(grounding_label, grounding_confidence),
        session_id="ablation-disabled",
        logger=logger_dis,
    )

    for entry in signal_trace:
        frame_bytes = bytes.fromhex(entry["frame_bytes_hex"])
        frame_event_id = f"ablation-disabled-frame-{entry['seq_no']}"
        ref = FrameRef(
            event_id=frame_event_id,
            timestamp_mono_ms=entry["timestamp_mono_ms"],
            frame_bytes=frame_bytes,
        )
        sidecar_dis.ingest_frame(ref)

    # Detector is disabled: deictic_reference is never set to True.
    result_dis = sidecar_dis.resolve("what is this?", deictic_reference=False)

    await logger_dis.stop()

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    grounding_en = [e for e in events_en if e.event_type == "deictic_grounding"]
    grounding_dis = [e for e in events_dis if e.event_type == "deictic_grounding"]

    # (a) Enabled path: exactly one grounding event.
    assert len(grounding_en) == enabled_cfg["expected_deictic_grounding_events"], (
        f"Enabled path: expected {enabled_cfg['expected_deictic_grounding_events']} "
        f"deictic_grounding event(s), got {len(grounding_en)}"
    )

    # (b) Disabled path: zero grounding events.
    assert len(grounding_dis) == disabled_cfg["expected_deictic_grounding_events"], (
        f"Disabled path: expected {disabled_cfg['expected_deictic_grounding_events']} "
        f"deictic_grounding event(s), got {len(grounding_dis)}"
    )

    # (c) Enabled path causal chain: grounding event caused_by includes both
    #     the deictic_classification event_id and a frame event_id.
    ge = grounding_en[0]
    assert deictic_en.event_id in ge.caused_by, (
        f"grounding event caused_by={ge.caused_by!r} missing deictic_classification id {deictic_en.event_id!r}"
    )
    assert result_en.frame_event_id is not None
    assert result_en.frame_event_id in ge.caused_by, (
        f"grounding event caused_by={ge.caused_by!r} missing frame event_id {result_en.frame_event_id!r}"
    )

    # (d) Paths are distinguishable: enabled produced a grounding result; disabled did not.
    assert result_en.frame_event_id is not None, (
        "Enabled path: grounding result must be resolvable"
    )
    assert result_dis.frame_event_id is None, (
        "Disabled path: grounding result must be not-resolvable"
    )
    assert result_en.label == grounding_label

    # Deictic classification events exist only in the enabled path.
    classif_en = [e for e in events_en if e.event_type == "deictic_classification"]
    classif_dis = [e for e in events_dis if e.event_type == "deictic_classification"]
    assert len(classif_en) == 1, (
        f"Enabled path: expected 1 deictic_classification event, got {len(classif_en)}"
    )
    assert len(classif_dis) == 0, (
        f"Disabled path: expected 0 deictic_classification events, got {len(classif_dis)}"
    )
