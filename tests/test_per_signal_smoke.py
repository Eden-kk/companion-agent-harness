"""Per-signal smoke tests — Wave 2-5 producer code-path coverage (v0.1j Task 17).

Each test asserts that the producer's code path was invoked at least once using
fakes/stubs; no real GPU or model weights are required.  Tests are skipped with
a documented reason when the producer is gated by an UNAVAILABLE marker.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from companion_harness.av_conflict_scorer import _NullAudioVisualConflictScorer
from companion_harness.deictic_detector import DeicticDetector, _NullDeicticModel
from companion_harness.native_duplex_eou import _NullNativeDuplexEouSource
from companion_harness.schemas import TurnSignal
from companion_harness.urgency_scorer import _NullUrgencyScorer
from companion_harness.vision_sidecar import (
    FrameRef,
    VisionSidecar,
    _NullGroundingModel,
    _NullSceneScorer,
)
from manual_test_console.live_pipeline import _make_policy_inputs_builder


def _turn_signal(p_done: float = 0.8) -> TurnSignal:
    return TurnSignal(
        detector="test",
        p_done=p_done,
        p_continue=1.0 - p_done,
        p_backchannel=0.0,
        confidence=1.0,
        evidence_event_ids=[],
    )


def _frame_ref(ts: int = 1000) -> FrameRef:
    return FrameRef(event_id=f"raw-video-{ts}", timestamp_mono_ms=ts, frame_bytes=b"\x00" * 4)


# ---------------------------------------------------------------------------
# 1. VisionSidecar — scene_change_score producer
# ---------------------------------------------------------------------------


def test_scene_change_score_producer_fires_at_least_once():
    """VisionSidecar.last_scene_change_score() returns non-None over a simulated session."""
    mock_scorer = MagicMock(return_value=0.42)
    sidecar = VisionSidecar(
        scene_scorer=mock_scorer,
        grounding_model=_NullGroundingModel(),
    )
    # Ingest two frames so the scorer is called (needs a previous frame).
    sidecar.ingest_frame(_frame_ref(1000))
    sidecar.ingest_frame(_frame_ref(2000))

    assert mock_scorer.called
    score = sidecar.last_scene_change_score()
    assert score is not None
    assert score == 0.42


# ---------------------------------------------------------------------------
# 2. AudioVisualConflictScorer — av_conflict_score producer
# ---------------------------------------------------------------------------


def test_av_conflict_scorer_fires():
    """policy_inputs_builder calls AudioVisualConflictScorer.score() at least once.

    # UNAVAILABLE: #168 — real cross-modal conflict scorer pending spec decision; null stub active.
    """
    mock_scorer = MagicMock()
    mock_scorer.score.return_value = 0.3

    builder = _make_policy_inputs_builder(None, av_scorer=mock_scorer)
    builder(_turn_signal(), [])

    assert mock_scorer.score.called


# ---------------------------------------------------------------------------
# 3. VisionSidecar — grounding_confidence producer
# ---------------------------------------------------------------------------


def test_grounding_confidence_fires():
    """VisionSidecar.grounding_confidence() accessor is invoked via policy_inputs_builder."""
    sidecar = VisionSidecar(
        scene_scorer=_NullSceneScorer(),
        grounding_model=_NullGroundingModel(),
    )
    mock_sidecar = MagicMock(wraps=sidecar)
    mock_sidecar.last_scene_change_score.return_value = 0.0
    mock_sidecar.grounding_confidence.return_value = 0.9

    builder = _make_policy_inputs_builder(mock_sidecar)
    inputs = builder(_turn_signal(), [])

    assert mock_sidecar.grounding_confidence.called
    assert inputs.grounding_confidence == 0.9


# ---------------------------------------------------------------------------
# 4. UrgencyScorer — urgency_score producer
# ---------------------------------------------------------------------------


def test_urgency_score_fires():
    """_NullUrgencyScorer.score() is invoked by the policy_inputs_builder seam.

    # UNAVAILABLE: #171 — real safety-risk classifier pending model selection; null stub active.
    """
    mock_scorer = MagicMock()
    mock_scorer.score.return_value = 0.0

    builder = _make_policy_inputs_builder(None, urgency_scorer=mock_scorer)
    builder(_turn_signal(), [])

    assert mock_scorer.score.called


# ---------------------------------------------------------------------------
# 5. NativeDuplexEouSource — EOU primary or fallback
# ---------------------------------------------------------------------------


def test_native_duplex_eou_fires_or_falls_back():
    """_NullNativeDuplexEouSource.get_eou_signal() code path executes without error.

    # UNAVAILABLE: #157 — libcudart blocker; null source always routes to fallback.
    The null stub is the active producer; this test confirms the code path runs.
    """
    source = _NullNativeDuplexEouSource()
    mock_source = MagicMock(wraps=source)

    result = mock_source.get_eou_signal()

    assert mock_source.get_eou_signal.called
    assert result is None  # null source routes to SmartTurn/VAD fallback


# ---------------------------------------------------------------------------
# 6. AddressingClassifier — addressing producer (wake-word safety-net)
# ---------------------------------------------------------------------------


def test_addressing_classifier_fires_or_falls_back():
    """WakeWordAddressingClassifier is invoked and returns an AddressingSignal.

    # UNAVAILABLE: #157 — libcudart blocker; MiniCPM-derived addressing unavailable.
    WakeWordAddressingClassifier is the active safety-net producer.
    """
    from companion_harness.addressing_classifier import (
        AddressingSignal,
        WakeWordAddressingClassifier,
    )

    classifier = WakeWordAddressingClassifier()
    mock_classifier = MagicMock(wraps=classifier)

    result = mock_classifier("hello companion", None, "user_addressing_agent")

    assert mock_classifier.called
    assert isinstance(result, AddressingSignal)


# ---------------------------------------------------------------------------
# 7. DeicticDetector — deictic_reference producer
# ---------------------------------------------------------------------------


def test_deictic_detector_fires():
    """DeicticDetector.classify() is invoked and returns a DeicticResult."""
    from companion_harness.deictic_detector import DeicticResult
    from companion_harness.event_logger import EventLogger

    async def _sink(evt: object) -> None:
        pass

    logger = EventLogger(_sink, maxsize=32)
    detector = DeicticDetector(
        model=_NullDeicticModel(),
        session_id="test-deictic-smoke",
        logger=logger,
    )
    mock_detector = MagicMock(wraps=detector)

    result = mock_detector.classify("look at that", caused_by=["evt-0"])

    assert mock_detector.classify.called
    assert isinstance(result, DeicticResult)
    assert result.is_deictic is False  # null model always returns False


# ---------------------------------------------------------------------------
# 8. SleepTimeAgent — optional memory-commit producer (wire_sleep_time_agent=True)
# ---------------------------------------------------------------------------


def test_sleep_time_agent_optional_when_enabled():
    """SleepTimeAgent is wired when wire_sleep_time_agent=True; skipped when False.

    # UNAVAILABLE: #188 — LLM-driven confidence/salience scorer; stub defaults used.
    """
    from companion_harness.sleep_time_agent import SleepTimeAgent

    async def _drain(_: object) -> None:
        pass

    from companion_harness.event_logger import EventLogger
    from manual_test_console.live_pipeline import build_live_pipeline
    from companion_harness.input_ingest import InputIngest
    from datetime import datetime, timezone
    from pathlib import Path
    import tempfile

    async def _build(wire: bool) -> bool:
        logger = EventLogger(_drain, maxsize=32)
        await logger.start()
        try:
            with tempfile.TemporaryDirectory() as td:
                ingest = InputIngest(logger, Path(td) / "blobs")
                session = ingest.open_session("smoke-sta")

                class _Fake:
                    def infer(self, audio_frame: bytes, video_frame: bytes | None = None):
                        return None

                    def set_context(self, items: list) -> None:
                        pass

                    async def infer_stream(self, frame_iter, caused_by, context_items=()):
                        async def _gen():
                            async for _ in frame_iter:
                                pass
                            return
                            yield  # pragma: no cover

                        return _gen()

                pipeline = build_live_pipeline(
                    session_id=session.session_id,
                    logger=logger,
                    ingest_session=session,
                    foreground_duplex_model=_Fake(),
                    use_stubs=True,
                    wire_sleep_time_agent=wire,
                )
                return pipeline.sleep_time_agent is not None
        finally:
            await logger.stop()

    import asyncio

    assert asyncio.run(_build(True)) is True
    assert asyncio.run(_build(False)) is False
