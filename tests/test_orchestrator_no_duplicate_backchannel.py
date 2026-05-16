"""Contract tests for Finding 9 generalisation: burst backchannel dispatch.

Round 4 (session a215abde) reproduced quadruple-dispatch on backchannel signals
(p_backchannel=0.9) — not just full_response — in three tight clusters:
  701444416/416/416/417 (4 decisions), 701444651/652/652 (3), 701445544/545/545 (3).

PR #260 replaced the done()-based guard with _decision_in_flight bool.
These tests verify:
  1. A burst of 4 backchannel-tier signals dedupes to exactly 1 policy_decision.
  2. A burst of 3 full_response-tier signals dedupes to exactly 1 policy_decision
     (cross-check against PR #260's original scope).
  3. A mixed burst (some backchannel, some full_response tier) dedupes to exactly 1.
  4. Single signals outside burst windows still fire normally.

Signals are injected directly onto _t2_inbox to guarantee all N arrive before T2
processes any of them — the exact race window the pre-#260 bug lived in.
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
# Shared helpers (minimal; self-contained per CLAUDE.md discipline)
# ---------------------------------------------------------------------------

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=1024), received


class _FakeVAD:
    def __init__(self) -> None:
        pass

    def __call__(self, frame: bytes) -> float:
        return 0.0  # no speech; we inject signals directly


class _FakeSmartTurn:
    def __call__(self, buf: bytes) -> tuple[float, float]:
        return 0.1, 0.9  # never fires EOU from audio


class _FakeBackchannel:
    def __call__(self, frame: bytes) -> float:
        return 0.0  # no backchannel from audio; injected directly


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
                content="test",
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="backchannel",
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
        user_speaking=False,
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
) -> StreamingRealtimeOrchestrator:
    session = ingest.open_session("test-client")
    vad = VADDetector(
        model=_FakeVAD(),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
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
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=asyncio.Queue(maxsize=64),
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


def _backchannel_signal(evt_id: str) -> TurnSignal:
    """TurnSignal that routes to backchannel action (p_backchannel=0.9 >= 0.7 threshold)."""
    return TurnSignal(
        detector="backchannel_classifier",
        p_done=0.8,
        p_continue=0.2,
        p_backchannel=0.9,
        confidence=0.9,
        evidence_event_ids=[evt_id],
    )


def _full_response_signal(evt_id: str) -> TurnSignal:
    """TurnSignal that routes to full_response action (p_backchannel=0.0, p_done=0.95)."""
    return TurnSignal(
        detector="smart_turn",
        p_done=0.95,
        p_continue=0.05,
        p_backchannel=0.0,
        confidence=0.95,
        evidence_event_ids=[evt_id],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_burst_4_backchannel_signals_dedupe_to_one(tmp_path):
    """Finding 9 Round 4 exact reproduction: 4 backchannel signals in one burst.

    Injects 4 TurnSignals with p_backchannel=0.9 directly onto _t2_inbox before
    T2 processes any.  Pre-#260 code produced 4 policy_decision events (quadruple);
    post-#260 code must produce exactly 1 plus 3 turn_signal_coalesced.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    orch = _build_orchestrator("test-bc-4burst", logger, ingest)
    await orch.start()

    fake_evt_id = "fake-backchannel-evt-001"
    # Inject all 4 before any asyncio yield — reproduces the burst window from a215abde.
    for _ in range(4):
        await orch._t2_inbox.put((_backchannel_signal(fake_evt_id), fake_evt_id))

    await asyncio.sleep(0.5)
    await orch.stop()

    policy_decisions = [e for e in received if e.event_type == "policy_decision"]
    coalesced = [e for e in received if e.event_type == "turn_signal_coalesced"]

    assert len(policy_decisions) == 1, (
        f"Expected 1 policy_decision for 4-backchannel burst, got {len(policy_decisions)}. "
        f"Finding 9 quadruple-dispatch bug present on backchannel path. "
        f"seq_nos={[e.seq_no for e in policy_decisions]}, "
        f"timestamps={[e.timestamp_mono_ms for e in policy_decisions]}"
    )
    assert len(coalesced) == 3, (
        f"Expected 3 turn_signal_coalesced (one per duplicate), got {len(coalesced)}"
    )
    # Verify the surviving decision is backchannel action type (not full_response).
    inline = policy_decisions[0].payload_inline or {}
    assert inline.get("action_type") == "backchannel", (
        f"Expected action_type=backchannel, got {inline.get('action_type')!r}"
    )
    assert inline.get("primary_reason_code") == "BACKCHANNEL_DETECTED", (
        f"Expected BACKCHANNEL_DETECTED, got {inline.get('primary_reason_code')!r}"
    )


@pytest.mark.asyncio
async def test_burst_3_full_response_signals_dedupe_to_one(tmp_path):
    """Cross-check: PR #260's original fix scope — 3 full_response-tier signals.

    Equivalent to test_concurrent_eou_and_addressing_signals_dedupe but uses
    the helpers from this file for consistency.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    orch = _build_orchestrator("test-fr-3burst", logger, ingest)
    await orch.start()

    fake_evt_id = "fake-full-response-evt-001"
    for _ in range(3):
        await orch._t2_inbox.put((_full_response_signal(fake_evt_id), fake_evt_id))

    await asyncio.sleep(0.5)
    await orch.stop()

    policy_decisions = [e for e in received if e.event_type == "policy_decision"]
    coalesced = [e for e in received if e.event_type == "turn_signal_coalesced"]

    assert len(policy_decisions) == 1, (
        f"Expected 1 policy_decision for 3-full_response burst, got {len(policy_decisions)}. "
        f"seq_nos={[e.seq_no for e in policy_decisions]}"
    )
    assert len(coalesced) == 2, (
        f"Expected 2 turn_signal_coalesced, got {len(coalesced)}"
    )


@pytest.mark.asyncio
async def test_mixed_signal_burst_dedupes_to_one(tmp_path):
    """Mixed burst: 2 backchannel + 2 full_response tier signals, same turn boundary.

    All four arrive before T2 processes any.  Only the first must produce a decision;
    the other three must be coalesced.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    orch = _build_orchestrator("test-mixed-burst", logger, ingest)
    await orch.start()

    fake_evt_id = "fake-mixed-evt-001"
    # Alternate backchannel / full_response to test ordering independence.
    for sig_fn in (
        _backchannel_signal,
        _full_response_signal,
        _backchannel_signal,
        _full_response_signal,
    ):
        await orch._t2_inbox.put((sig_fn(fake_evt_id), fake_evt_id))

    await asyncio.sleep(0.5)
    await orch.stop()

    policy_decisions = [e for e in received if e.event_type == "policy_decision"]
    coalesced = [e for e in received if e.event_type == "turn_signal_coalesced"]

    assert len(policy_decisions) == 1, (
        f"Expected 1 policy_decision for mixed burst, got {len(policy_decisions)}. "
        f"seq_nos={[e.seq_no for e in policy_decisions]}"
    )
    assert len(coalesced) == 3, (
        f"Expected 3 turn_signal_coalesced, got {len(coalesced)}"
    )


@pytest.mark.asyncio
async def test_singles_outside_burst_still_dispatch_correctly(tmp_path):
    """Signals arriving in separate asyncio ticks (non-burst) each produce a decision.

    After T4 resets _decision_in_flight, the next signal must pass the guard.
    Injects two signals with a sleep between them so T4 runs in the gap.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    orch = _build_orchestrator("test-singles", logger, ingest)
    await orch.start()

    evt_id_1 = "fake-single-evt-001"
    evt_id_2 = "fake-single-evt-002"

    # First signal — T4 runs and resets _decision_in_flight before the second arrives.
    await orch._t2_inbox.put((_backchannel_signal(evt_id_1), evt_id_1))
    await asyncio.sleep(0.4)  # enough for T4 to dequeue + reset flag

    # Second signal — should see _decision_in_flight=False and produce its own decision.
    await orch._t2_inbox.put((_backchannel_signal(evt_id_2), evt_id_2))
    await asyncio.sleep(0.4)

    await orch.stop()

    policy_decisions = [e for e in received if e.event_type == "policy_decision"]
    coalesced = [e for e in received if e.event_type == "turn_signal_coalesced"]

    assert len(policy_decisions) == 2, (
        f"Expected 2 policy_decisions (one per signal), got {len(policy_decisions)}. "
        f"The _decision_in_flight flag may not be cleared by T4 correctly."
    )
    assert len(coalesced) == 0, (
        f"Expected 0 coalesced events for separated singles, got {len(coalesced)}"
    )
