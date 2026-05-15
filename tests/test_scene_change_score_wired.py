"""Contract tests for scene_change_score wiring (v0.1j Task 3).

Success criterion:
  pytest tests/test_scene_change_score_wired.py -v  # 3 tests, all green
"""

from __future__ import annotations

import inspect

from companion_harness.schemas import TurnSignal
from companion_harness.vision_sidecar import (
    FrameRef,
    SceneScorer,
    VisionSidecar,
    _NullSceneScorer,
)
from manual_test_console.live_pipeline import _make_live_policy_inputs_builder


def _make_signal() -> TurnSignal:
    return TurnSignal(
        detector="test",
        p_done=0.9,
        p_continue=0.05,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=[],
    )


def _make_sidecar(scorer: SceneScorer) -> VisionSidecar:
    class _FakeGroundingModel:
        def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
            return "", 0.0

    return VisionSidecar(scene_scorer=scorer, grounding_model=_FakeGroundingModel())


def test_scene_change_score_uses_vision_sidecar_accessor() -> None:
    """PolicyInputs.scene_change_score must reflect vision_sidecar.last_scene_change_score(),
    not a hardcoded literal.
    """
    class _ScriptedScorer:
        def __call__(self, prev_frame: bytes, curr_frame: bytes) -> float:
            return 0.75

    sidecar = _make_sidecar(_ScriptedScorer())
    sidecar.ingest_frame(FrameRef(event_id="ev-a", timestamp_mono_ms=1_000, frame_bytes=b"\x01"))
    sidecar.ingest_frame(FrameRef(event_id="ev-b", timestamp_mono_ms=2_000, frame_bytes=b"\x02"))

    assert sidecar.last_scene_change_score() == 0.75

    builder = _make_live_policy_inputs_builder(vision_sidecar=sidecar)
    sig = _make_signal()
    inputs = builder(sig, [sig])
    assert inputs.scene_change_score == 0.75


def test_null_scene_scorer_returns_zero_with_marker() -> None:
    """_NullSceneScorer must return 0.0 and carry the UNAVAILABLE marker in source."""
    scorer = _NullSceneScorer()
    result = scorer(b"\x00", b"\x01")
    assert result == 0.0

    source = inspect.getsource(_NullSceneScorer)
    assert "UNAVAILABLE: #166" in source, (
        "_NullSceneScorer source must contain '# UNAVAILABLE: #166' marker"
    )


def test_scene_scorer_protocol_runtime_checkable() -> None:
    """_NullSceneScorer must satisfy the SceneScorer Protocol at runtime."""
    assert isinstance(_NullSceneScorer(), SceneScorer)
