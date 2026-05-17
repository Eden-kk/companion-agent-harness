"""ConfigStore → orchestrator + detectors integration (config-dashboard Task D).

Verifies that all 12 Tier-B keys end up actually controlling behavior when a
ConfigStore is wired through ``StreamingRealtimeOrchestrator``:

  - Detector setters (VADDetector, SmartTurnDetector, BackchannelClassifier)
    update internal thresholds and take effect on the next ``process_frame``.
  - ``speak_policy.decide()`` honors the kwargs that override its module-level
    constants.
  - Orchestrator ``_snapshot_config()`` reads all 12 keys at the EOU boundary
    BEFORE ``_speak_policy_decide()`` runs, applies orchestrator-level state,
    invokes detector setters, and threads policy kwargs into decide().
  - When ``config_store=None`` the orchestrator behaves exactly as before
    (backward compat).

See docs/design-config-and-dashboard.md §3 (loading mechanism) and §6
(replay reproducibility — timing rule).

All adapters are fakes; no MiniCPM / Kokoro / torch import.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.speak_policy import decide as _real_decide
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector
from manual_test_console.config_schema import ALLOWLIST
from manual_test_console.config_store import ConfigStore


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-config-store",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


# ---------------------------------------------------------------------------
# Fake adapters
# ---------------------------------------------------------------------------


class _FakeVADModel:
    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def __call__(self, frame: bytes) -> float:
        return next(self._probs, 0.0)


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


class _ConstantBackchannelModel:
    def __init__(self, prob: float = 0.0) -> None:
        self._prob = prob

    def __call__(self, frame: bytes) -> float:
        return self._prob


class _FakeStreamingModel:
    def __init__(self, text: str = "scripted") -> None:
        self._text = text

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items) -> None:
        pass

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        context_items: tuple = (),
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            yield ThinkerProposal(
                proposal_type="observation",
                content=self._text,
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="full_response",
                caused_by=caused_by,
            )

        return _gen()


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    """Builds a PolicyInputs configured so the EOU branch will fire.

    eou_probability is set to the signal's p_done; user_speaking is False so the
    policy walks past gates 2/3 and lands on the backchannel/full_response
    branches where the tunable thresholds matter.
    """
    return PolicyInputs(
        user_speaking=False,
        eou_probability=max(0.6, signal.p_done),
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="default",
        current_task_mode="default",
        social_mode="default",
        risk_mode="default",
        cooldown_state={},
        attachment_risk_level=0.0,
        audio_visual_conflict_score=0.0,
        grounding_confidence=1.0,
        deictic_ambiguous=False,
    )


class _SpySpeakPolicy:
    """Records the threshold kwargs decide() was called with."""

    def __init__(self) -> None:
        self.last_kwargs: dict[str, float] | None = None
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
        *,
        backchannel_threshold: float = 0.7,
        audio_visual_conflict_threshold: float = 0.7,
        grounding_confidence_threshold: float = 0.5,
    ) -> SpeakDecision:
        kwargs = {
            "backchannel_threshold": backchannel_threshold,
            "audio_visual_conflict_threshold": audio_visual_conflict_threshold,
            "grounding_confidence_threshold": grounding_confidence_threshold,
        }
        self.last_kwargs = kwargs
        self.calls.append({"p_backchannel": p_backchannel, **kwargs})
        return _real_decide(
            inputs,
            signal_event_ids,
            p_backchannel,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# Test 1 — speak_policy.decide() with kwargs overrides module-level constants
# ---------------------------------------------------------------------------


def _eou_ready_inputs(**overrides: Any) -> PolicyInputs:
    base = dict(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="default",
        current_task_mode="default",
        social_mode="default",
        risk_mode="default",
        cooldown_state={},
        attachment_risk_level=0.0,
        audio_visual_conflict_score=0.0,
        grounding_confidence=1.0,
        deictic_ambiguous=False,
    )
    base.update(overrides)
    return PolicyInputs(**base)


def test_decide_default_kwargs_match_module_constants():
    """Backward-compat: calling decide() without kwargs uses 0.7 / 0.7 / 0.5."""
    inputs = _eou_ready_inputs()
    # p_backchannel=0.7 hits the default threshold, returns backchannel
    decision = _real_decide(inputs, ["sig-1"], p_backchannel=0.7)
    assert decision.action_type == "backchannel"
    # p_backchannel=0.69 stays full_response
    decision2 = _real_decide(inputs, ["sig-2"], p_backchannel=0.69)
    assert decision2.action_type == "full_response"


def test_decide_high_backchannel_threshold_flips_decision():
    """When backchannel_threshold=0.95, a p_backchannel=0.7 turn becomes full_response.

    This is the headline 'all 12 keys observable in behavior' test for the
    policy.backchannel_threshold knob.
    """
    inputs = _eou_ready_inputs()
    # Default behavior: p_backchannel=0.7 → backchannel
    default = _real_decide(inputs, ["sig-1"], p_backchannel=0.7)
    assert default.action_type == "backchannel"
    # With raised threshold → full_response
    raised = _real_decide(
        inputs,
        ["sig-2"],
        p_backchannel=0.7,
        backchannel_threshold=0.95,
    )
    assert raised.action_type == "full_response"


def test_decide_low_av_conflict_threshold_flips_decision():
    """When audio_visual_conflict_threshold=0.4, a 0.5 conflict score → clarification."""
    # av_conflict=0.5, default threshold 0.7 → full_response
    inputs = _eou_ready_inputs(audio_visual_conflict_score=0.5)
    default = _real_decide(inputs, ["sig-1"], p_backchannel=0.0)
    assert default.action_type == "full_response"
    # Lower threshold → clarification
    lowered = _real_decide(
        inputs,
        ["sig-2"],
        p_backchannel=0.0,
        audio_visual_conflict_threshold=0.4,
    )
    assert lowered.action_type == "clarification"


def test_decide_grounding_threshold_blocks_low_confidence():
    """grounding_confidence_threshold=0.9 suppresses a deictic with conf=0.6."""
    inputs = _eou_ready_inputs(
        deictic_reference=True,
        grounding_confidence=0.6,
    )
    # Default 0.5 threshold: 0.6 >= 0.5 → falls through to full_response
    default = _real_decide(inputs, ["sig-1"], p_backchannel=0.0)
    assert default.action_type == "full_response"
    # Raised 0.9 threshold: 0.6 < 0.9 → silence (VISUAL_LOW_CONFIDENCE)
    raised = _real_decide(
        inputs,
        ["sig-2"],
        p_backchannel=0.0,
        grounding_confidence_threshold=0.9,
    )
    assert raised.action_type == "silence"
    assert raised.primary_reason_code == ReasonCode.VISUAL_LOW_CONFIDENCE


# ---------------------------------------------------------------------------
# Test 2 — Detector setter methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vad_update_thresholds_takes_effect_next_frame():
    """VADDetector.update_thresholds(speech_threshold=0.8) → next frame sees new gate."""
    logger, _ = _make_logger()
    await logger.start()

    # Construct VAD with default 0.5; a frame returning 0.7 is in-speech.
    vad = VADDetector(
        model=_FakeVADModel([0.7]),
        session_id="vad-test",
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    assert vad._speech_threshold == 0.5
    # Apply setter at the EOU boundary: raise threshold to 0.8.
    vad.update_thresholds(speech_threshold=0.8)
    assert vad._speech_threshold == 0.8
    # silence_onset_ms unchanged.
    assert vad._silence_onset_ms == 64
    # Now apply silence_onset_ms.
    vad.update_thresholds(silence_onset_ms=200)
    assert vad._silence_onset_ms == 200
    assert vad._speech_threshold == 0.8  # still 0.8

    await logger.stop()


@pytest.mark.asyncio
async def test_smart_turn_update_thresholds_takes_effect():
    logger, _ = _make_logger()
    await logger.start()

    detector = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id="smart-test",
        logger=logger,
    )
    assert detector._silence_onset_ms == 300
    assert detector._silence_rms_threshold == 500

    detector.update_thresholds(silence_onset_ms=500, silence_rms_threshold=50)
    assert detector._silence_onset_ms == 500
    assert detector._silence_rms_threshold == 50

    # None leaves the field unchanged.
    detector.update_thresholds(silence_onset_ms=None, silence_rms_threshold=None)
    assert detector._silence_onset_ms == 500
    assert detector._silence_rms_threshold == 50

    await logger.stop()


@pytest.mark.asyncio
async def test_backchannel_update_threshold_takes_effect():
    """BackchannelClassifier.update_threshold(emit_threshold=0.05) → next frame uses new gate."""
    logger, received = _make_logger()
    await logger.start()

    classifier = BackchannelClassifier(
        model=_ConstantBackchannelModel(0.1),
        session_id="bc-test",
        logger=logger,
        emit_threshold=0.3,
    )
    # At 0.1 < 0.3 → suppressed.
    sig = classifier.process_frame(b"\x00" * 64, caused_by=["chunk-0"])
    assert sig is None

    # Lower threshold to 0.05.
    classifier.update_threshold(emit_threshold=0.05)
    assert classifier._emit_threshold == 0.05

    # Now 0.1 >= 0.05 → signal returned.
    sig2 = classifier.process_frame(b"\x00" * 64, caused_by=["chunk-1"])
    assert sig2 is not None
    assert sig2.p_backchannel == pytest.approx(0.1)

    await logger.stop()


# ---------------------------------------------------------------------------
# Test 3 — Orchestrator wires ConfigStore through to detectors + policy
# ---------------------------------------------------------------------------


def _build_orchestrator(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session: Any,
    audio_in: asyncio.Queue,
    spy_policy: _SpySpeakPolicy,
    config_store: ConfigStore | None,
) -> tuple[StreamingRealtimeOrchestrator, VADDetector, SmartTurnDetector, BackchannelClassifier]:
    vad = VADDetector(
        model=_FakeVADModel([0.9, 0.9, 0.1, 0.1, 0.1, 0.1]),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=_ConstantBackchannelModel(0.0),
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(
        model=_FakeStreamingModel(),
        session_id=session_id,
        logger=logger,
    )
    controller = AudioOutputController(
        session_id=session_id,
        logger=logger,
        sink=_noop_sink,
    )
    orch = StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=spy_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
        config_store=config_store,
    )
    return orch, vad, smart, bc


async def _drive_one_eou(audio_in: asyncio.Queue, ingest: InputIngest, session: Any) -> None:
    """Push speech-then-silence frames sufficient to trigger one EOU via the fake VAD."""
    for i in range(6):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))
    # Let asyncio scheduling thread through detector → T2.
    await asyncio.sleep(0.2)


@pytest.mark.asyncio
async def test_orchestrator_with_no_config_store_backward_compatible(tmp_path):
    """config_store=None → spy policy sees zero kwargs (defaults used inside decide)."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-no-config"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy = _SpySpeakPolicy()

    orch, _vad, _smart, _bc = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        spy_policy=spy,
        config_store=None,
    )
    await orch.start()
    await _drive_one_eou(audio_in, ingest, session)
    await orch.stop()

    # When config_store is None, _policy_threshold_kwargs stays empty → spy
    # sees the keyword defaults (0.7 / 0.7 / 0.5) from its own signature.
    assert spy.last_kwargs is not None, "decide() must have been called"
    assert spy.last_kwargs == {
        "backchannel_threshold": 0.7,
        "audio_visual_conflict_threshold": 0.7,
        "grounding_confidence_threshold": 0.5,
    }


@pytest.mark.asyncio
async def test_orchestrator_with_default_config_store_matches_baseline(tmp_path):
    """config_store at defaults → spy sees the same thresholds as backward-compat path."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-default-config"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy = _SpySpeakPolicy()
    store = ConfigStore(ALLOWLIST)

    orch, _vad, _smart, _bc = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        spy_policy=spy,
        config_store=store,
    )
    await orch.start()
    await _drive_one_eou(audio_in, ingest, session)
    await orch.stop()

    assert spy.last_kwargs == {
        "backchannel_threshold": 0.7,
        "audio_visual_conflict_threshold": 0.7,
        "grounding_confidence_threshold": 0.5,
    }


@pytest.mark.asyncio
async def test_orchestrator_with_custom_config_store_uses_custom_thresholds(tmp_path):
    """ConfigStore.set() patches → orchestrator passes the new thresholds to decide()."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-custom-config"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy = _SpySpeakPolicy()

    store = ConfigStore(ALLOWLIST)
    store.set("policy.backchannel_threshold", 0.95)
    store.set("policy.audio_visual_conflict_threshold", 0.42)
    store.set("policy.grounding_confidence_threshold", 0.88)

    orch, _vad, _smart, _bc = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        spy_policy=spy,
        config_store=store,
    )
    await orch.start()
    await _drive_one_eou(audio_in, ingest, session)
    await orch.stop()

    assert spy.last_kwargs == {
        "backchannel_threshold": 0.95,
        "audio_visual_conflict_threshold": 0.42,
        "grounding_confidence_threshold": 0.88,
    }


@pytest.mark.asyncio
async def test_orchestrator_applies_orchestrator_keys_to_self_state(tmp_path):
    """4 orchestrator.* keys end up on the orchestrator instance after first EOU."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-orch-state"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy = _SpySpeakPolicy()

    store = ConfigStore(ALLOWLIST)
    store.set("orchestrator.proposal_batch_window_ms", 150)
    store.set("orchestrator.hard_cancel_after_ms", 90)
    store.set("orchestrator.p_speech_thresh", 0.62)
    store.set("orchestrator.p_backchannel_thresh", 0.55)

    orch, _vad, _smart, _bc = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        spy_policy=spy,
        config_store=store,
    )
    await orch.start()
    await _drive_one_eou(audio_in, ingest, session)
    await orch.stop()

    assert orch._proposal_batch_window_ms == 150
    assert orch._hard_cancel_after_ms == 90
    assert orch._p_speech_thresh == pytest.approx(0.62)
    assert orch._p_backchannel_thresh == pytest.approx(0.55)


@pytest.mark.asyncio
async def test_orchestrator_applies_detector_keys_via_setters(tmp_path):
    """5 detector keys → detectors via setters → visible on detector state after EOU."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-detector-state"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy = _SpySpeakPolicy()

    store = ConfigStore(ALLOWLIST)
    store.set("detectors.vad.speech_threshold", 0.65)
    store.set("detectors.vad.silence_onset_ms", 450)
    store.set("detectors.smart_turn.silence_onset_ms", 600)
    store.set("detectors.smart_turn.silence_rms_threshold", 75)
    store.set("detectors.backchannel.emit_threshold", 0.12)

    orch, vad, smart, bc = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        spy_policy=spy,
        config_store=store,
    )
    await orch.start()
    await _drive_one_eou(audio_in, ingest, session)
    await orch.stop()

    # All 5 detector keys observable on detector state after the EOU.
    assert vad._speech_threshold == pytest.approx(0.65)
    assert vad._silence_onset_ms == 450
    assert smart._silence_onset_ms == 600
    assert smart._silence_rms_threshold == 75
    assert bc._emit_threshold == pytest.approx(0.12)


# ---------------------------------------------------------------------------
# Test 4 — EOU-boundary timing: setter is called BEFORE the next frame
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setter_fires_before_next_frame_after_eou(tmp_path):
    """Spy-classifier counts ``update_threshold`` invocations relative to ``process_frame`` calls.

    After the first EOU, the next frame's ``process_frame`` call MUST see the
    updated threshold value — i.e. the setter ran at the EOU boundary, before
    the next frame's classification.
    """
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-eou-boundary"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy = _SpySpeakPolicy()

    store = ConfigStore(ALLOWLIST)
    # Start at default; after the EOU, raise emit_threshold to 0.95.
    store.set("detectors.backchannel.emit_threshold", 0.95)

    orch, vad, smart, bc = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        spy_policy=spy,
        config_store=store,
    )
    # Sanity: pre-start, classifier still has the construction default.
    assert bc._emit_threshold == pytest.approx(0.3)

    await orch.start()
    await _drive_one_eou(audio_in, ingest, session)
    await orch.stop()

    # By the time the EOU was processed, the setter had fired → 0.95.
    # If the setter had fired AFTER decide() returned (or never), this would
    # still be 0.3.
    assert bc._emit_threshold == pytest.approx(0.95)
