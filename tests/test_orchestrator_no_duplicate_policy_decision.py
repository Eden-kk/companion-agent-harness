"""Regression tests for Finding 9: duplicate policy_decision at turn boundaries.

Root cause: _pending_decision_future.done() returned True immediately after
set_result() was called (before T4 consumed the future), causing the coalescing
guard to let subsequent same-turn signals through as duplicate decisions.

Fix: _decision_in_flight bool flag — set in T2 when a TurnSignal passes the guard,
cleared in T4 after await _policy_decisions.get().  T4's get() is the reliable
"decision consumed" signal: any TurnSignal that T2 dequeues before T4 does its
get() will see _decision_in_flight=True and emit turn_signal_coalesced.

Bug signature (Finding 9): two or more policy_decision events at identical
timestamp_mono_ms values, different seq_no, all action=full_response/EOU_CONFIRMED.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import Event, PolicyInputs, SpeakDecision, ThinkerProposal, TurnSignal
from companion_harness.speak_policy import decide as speak_policy_decide
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Shared helpers (self-contained per CLAUDE.md discipline)
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=1024), received


def _pcm(n: int = 512) -> bytes:
    return b"\x00" * n


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-client",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


class _FakeVAD:
    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def __call__(self, frame: bytes) -> float:
        return next(self._probs, 0.0)


class _FakeSmartTurn:
    def __call__(self, buf: bytes) -> tuple[float, float]:
        return 0.1, 0.9  # never fires EOU


class _FakeBackchannel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
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
                content="test response",
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="full_response",
                caused_by=caused_by,
            )

        return _gen()


class _SilentTts:
    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b"\x00" * 160


async def _noop_sink(chunk: bytes) -> None:
    pass


def _build_policy_inputs(signal: TurnSignal, history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=signal.p_done <= signal.p_continue,
        eou_probability=signal.p_done,
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


def _build_orchestrator(
    session_id: str,
    logger: EventLogger,
    ingest: InputIngest,
    vad_probs: list[float],
    audio_in: asyncio.Queue | None = None,
) -> tuple[StreamingRealtimeOrchestrator, object, asyncio.Queue]:
    session = ingest.open_session("test-client")
    if audio_in is None:
        audio_in = asyncio.Queue(maxsize=64)

    vad = VADDetector(
        model=_FakeVAD(vad_probs),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,  # 2 frames of silence at 32ms/frame
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=_FakeSmartTurn(),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=_FakeBackchannel(),
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
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=speak_policy_decide,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=_SilentTts(),  # type: ignore[arg-type]
        proposal_batch_window_ms=200,
    )
    return orch, session, audio_in


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_eou_produces_single_policy_decision(tmp_path):
    """One EOU boundary must produce exactly one policy_decision event (not two or three).

    Bug signature: multiple policy_decision events at bit-identical or near-identical
    timestamp_mono_ms with different seq_no — Finding 9 symptom.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    orch, session, audio_in = _build_orchestrator(
        session_id="test-single-eou",
        logger=logger,
        ingest=ingest,
        # VAD: 4 speech frames then 3 silence frames → 1 EOU boundary
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
    )

    await orch.start()
    for i in range(7):
        evt = ingest.ingest_chunk(session, _pcm(), _meta(i))
        await audio_in.put((_pcm(), evt.event_id))
    await asyncio.sleep(0.4)
    await orch.stop()

    policy_decisions = [e for e in received if e.event_type == "policy_decision"]
    assert len(policy_decisions) == 1, (
        f"Expected exactly 1 policy_decision per EOU boundary, got {len(policy_decisions)}. "
        f"Duplicate seq_nos: {[e.seq_no for e in policy_decisions]}, "
        f"timestamps: {[e.timestamp_mono_ms for e in policy_decisions]}"
    )


@pytest.mark.asyncio
async def test_concurrent_eou_and_addressing_signals_dedupe(tmp_path):
    """Signals for the same turn injected back-to-back produce only 1 policy_decision.

    Reproduces the pre-fix scenario: _pending_decision_future.done() was True
    (set_result had been called but T4 had not yet consumed the future via get()),
    so subsequent same-turn signals bypassed the coalescing guard.

    This test injects three TurnSignal items directly onto _t2_inbox to guarantee
    that all three arrive at T2 before T4 has a chance to run — exactly the race
    window the bug lived in (no asyncio yield between T2's put() and T2's next get()
    when _t2_inbox is non-empty).
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    orch, session, audio_in = _build_orchestrator(
        session_id="test-dedup",
        logger=logger,
        ingest=ingest,
        vad_probs=[],  # no audio-driven signals; injected directly below
    )

    await orch.start()

    # Inject three TurnSignal items for the same turn boundary, each pretending
    # to be a different detector.  All three land in _t2_inbox before T2 processes
    # any of them, reproducing the Finding 9 race window.
    fake_evt_id = "fake-eou-event-id-001"
    for detector_name in ("vad", "smart_turn", "backchannel"):
        sig = TurnSignal(
            detector=detector_name,
            p_done=0.95,
            p_continue=0.05,
            p_backchannel=0.0,
            confidence=0.95,
            evidence_event_ids=[fake_evt_id],
        )
        await orch._t2_inbox.put((sig, fake_evt_id))

    await asyncio.sleep(0.5)
    await orch.stop()

    policy_decisions = [e for e in received if e.event_type == "policy_decision"]
    coalesced = [e for e in received if e.event_type == "turn_signal_coalesced"]

    assert len(policy_decisions) == 1, (
        f"Expected 1 policy_decision but got {len(policy_decisions)}: "
        f"seq_nos={[e.seq_no for e in policy_decisions]}, "
        f"timestamps={[e.timestamp_mono_ms for e in policy_decisions]}. "
        f"Finding 9 duplicate-dispatch bug still present."
    )
    # The two duplicate signals must have been coalesced (not silently dropped).
    assert len(coalesced) == 2, (
        f"Expected 2 turn_signal_coalesced events (one per duplicate signal), "
        f"got {len(coalesced)}"
    )
