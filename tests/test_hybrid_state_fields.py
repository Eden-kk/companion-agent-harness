"""Tests for the inline hybrid state machine on RealtimeOrchestrator (Option C Stage 2).

Success criterion: legal transitions pass; illegal transitions raise AssertionError;
self-transitions are no-ops.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, AsyncGenerator
from datetime import datetime, timezone
from typing import Any

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, PolicyInputs, SpeakDecision, ThinkerProposal
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


class _NullModel:
    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    def set_context(self, items: Any) -> None:
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
            return
            yield  # noqa: unreachable

        return _gen()


class _NullTts:
    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b""


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=512), received


def _make_orch(tmp_path, *, use_hybrid: bool = True) -> StreamingRealtimeOrchestrator:
    logger, _ = _make_logger()
    ingest = InputIngest(logger, tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)
    session_id = "test-session"
    vad = VADDetector(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=lambda _: (0.1, 0.9),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(model=_NullModel(), session_id=session_id, logger=logger)

    async def _noop_sink(chunk: bytes) -> None:
        pass

    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink)
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=lambda sig, hist: PolicyInputs(
            user_speaking=True,
            eou_probability=0.0,
            assistant_speaking=False,
            scene_change_score=0.0,
            deictic_reference=False,
            user_addressed_agent=None,
            urgency_score=0.0,
            proactivity_budget_remaining={},
            privacy_mode="normal",
            current_task_mode="normal",
            social_mode="user_addressing_agent",
            risk_mode="normal",
            cooldown_state={},
            attachment_risk_level=0.0,
        ),
        speak_policy=lambda inputs, sids, p=0.0: SpeakDecision(
            action_type="silence",
            primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(sids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        ),
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=_NullTts(),
        use_hybrid=use_hybrid,
    )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_initial_state_is_ambient_duplex(tmp_path) -> None:
    orch = _make_orch(tmp_path)
    assert orch._hybrid_state == "AMBIENT_DUPLEX"


# ---------------------------------------------------------------------------
# Legal transitions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transition", [
    ("AMBIENT_DUPLEX",      "EOU_PENDING_SWITCH"),
    ("EOU_PENDING_SWITCH",  "CHAT_STREAMING"),
    ("EOU_PENDING_SWITCH",  "AMBIENT_DUPLEX"),
    ("CHAT_STREAMING",      "RESETTING_TO_DUPLEX"),
    ("RESETTING_TO_DUPLEX", "AMBIENT_DUPLEX"),
])
def test_legal_transition_succeeds(tmp_path, transition) -> None:
    old_state, new_state = transition
    orch = _make_orch(tmp_path)
    orch._hybrid_state = old_state
    orch._transition_hybrid_state(new_state)
    assert orch._hybrid_state == new_state


# ---------------------------------------------------------------------------
# Illegal transitions
# ---------------------------------------------------------------------------


_ALL_STATES = ["AMBIENT_DUPLEX", "EOU_PENDING_SWITCH", "CHAT_STREAMING", "RESETTING_TO_DUPLEX"]
_LEGAL = frozenset({
    ("AMBIENT_DUPLEX",      "EOU_PENDING_SWITCH"),
    ("EOU_PENDING_SWITCH",  "CHAT_STREAMING"),
    ("EOU_PENDING_SWITCH",  "AMBIENT_DUPLEX"),
    ("CHAT_STREAMING",      "RESETTING_TO_DUPLEX"),
    ("RESETTING_TO_DUPLEX", "AMBIENT_DUPLEX"),
})
_ILLEGAL = [
    (a, b)
    for a in _ALL_STATES
    for b in _ALL_STATES
    if a != b and (a, b) not in _LEGAL
]


@pytest.mark.parametrize("transition", _ILLEGAL)
def test_illegal_transition_raises(tmp_path, transition) -> None:
    old_state, new_state = transition
    orch = _make_orch(tmp_path)
    orch._hybrid_state = old_state
    with pytest.raises(AssertionError):
        orch._transition_hybrid_state(new_state)


# ---------------------------------------------------------------------------
# Self-transitions are no-ops
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", _ALL_STATES)
def test_self_transition_is_noop(tmp_path, state) -> None:
    orch = _make_orch(tmp_path)
    orch._hybrid_state = state
    orch._transition_hybrid_state(state)  # must not raise
    assert orch._hybrid_state == state
