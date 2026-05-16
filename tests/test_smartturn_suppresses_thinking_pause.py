"""SmartTurn veto suppresses mid-utterance thinking pauses (closes Finding 13).

SmartTurn v3 must hold the EOU gate open across a 1.5s thinking pause — i.e.,
when SmartTurn fires with p_continue > p_done, the VAD EOU signal from the same
silence window must NOT reach the policy gate.

Scenario (handbook §2.5 scenario E):
  Operator says "I think" then pauses ~1.5s, then continues.
  Expected: only ONE policy_decision fires (at the real end), not one during the pause.

Root cause closed: VAD was enqueued before SmartTurn in _detector_fanout_task.
T2 processed VAD's high-p_done signal, called policy, then coalesced SmartTurn's
veto.  Fix: SmartTurn fires with p_continue > p_done → VAD signal suppressed in T1.
"""

from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

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
from companion_harness.tts_adapter import SilentTtsAdapter
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _speech_frame(n_samples: int = 256) -> bytes:
    """PCM-16 high-energy frame — RMS >> 100, VAD sees speech."""
    sample = struct.pack("<h", 2000)
    return sample * n_samples


def _silence_frame(n_samples: int = 256) -> bytes:
    """PCM-16 zero-energy frame — VAD and SmartTurn both see silence."""
    return b"\x00" * (n_samples * 2)


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-smartturn-veto",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _NoProposalStreamingModel:
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
            return
            yield  # make it a generator

        return _gen()


class _CapturingPolicy:
    """Records every PolicyInputs passed to decide(); always returns silence."""

    def __init__(self) -> None:
        self.calls: list[PolicyInputs] = []

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        self.calls.append(inputs)
        return SpeakDecision(
            action_type="silence",
            primary_reason_code=ReasonCode.NOT_ADDRESSED_TO_AGENT,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=signal.p_done <= signal.p_continue,
        eou_probability=signal.p_done,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="normal",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    smart_turn_model,
    speak_policy,
    trace_dir: Path,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=_ScriptedVADModel(vad_probs),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,   # 2 frames × 32ms
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=smart_turn_model,
        session_id=session_id,
        logger=logger,
        silence_onset_ms=64,   # 2 frames × 32ms
        frame_duration_ms=32,
    )
    bc = BackchannelClassifier(
        model=_FakeBackchannelModel(),
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(
        model=_NoProposalStreamingModel(),
        session_id=session_id,
        logger=logger,
    )
    aoc = AudioOutputController(
        session_id=session_id,
        logger=logger,
        sink=_noop_sink,
    )
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=speak_policy,
        foreground_model=fg,
        audio_output=aoc,
        tts_adapter=SilentTtsAdapter(),
        proposal_batch_window_ms=30,
        decision_trace_dir=trace_dir,
    )


class _ScriptedVADModel:
    def __init__(self, probs: list[float]) -> None:
        self._probs = probs
        self._idx = 0

    def __call__(self, frame: bytes) -> float:
        v = self._probs[self._idx] if self._idx < len(self._probs) else 0.0
        self._idx += 1
        return v


class _ScriptedSmartTurnModel:
    """Returns scripted (p_done, p_continue) pairs per invocation."""

    def __init__(self, pairs: list[tuple[float, float]]) -> None:
        self._pairs = pairs
        self._idx = 0

    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        pair = self._pairs[self._idx % len(self._pairs)]
        self._idx += 1
        return pair


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    frames: list[bytes],
) -> None:
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta(i))
        await audio_in.put((frame, evt.event_id))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_thinking_pause_does_not_fire_eou(tmp_path: Path):
    """Scenario E: mid-utterance 1.5s pause suppressed by SmartTurn veto.

    Audio trace: 5 speech frames → 3 silence frames (thinking pause)
    → 5 speech frames → 3 silence frames (real end).

    SmartTurn script: first invocation → p_continue=0.8 (thinking pause);
                      second invocation → p_done=0.9 (real end).

    Expected: exactly ONE policy_decision (on the real end, not the pause).
    Regression guard: vad_signal_suppressed_by_smart_turn event emitted at pause.
    """
    logger, received = _make_logger()
    await logger.start()

    policy = _CapturingPolicy()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-veto-thinking-pause")

    # VAD sees: speech (5 frames) → silence (5 frames) → speech (5 frames) → silence (5 frames)
    vad_probs = [0.9] * 5 + [0.0] * 5 + [0.9] * 5 + [0.0] * 5

    # SmartTurn: first silence window = thinking pause; second = real end
    smart_model = _ScriptedSmartTurnModel([(0.2, 0.8), (0.9, 0.1)])

    orch = _build_orch(
        session_id="test-veto-tp",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        smart_turn_model=smart_model,
        speak_policy=policy,
        trace_dir=tmp_path,
    )

    await orch.start()

    frames = (
        [_speech_frame()] * 5
        + [_silence_frame()] * 5
        + [_speech_frame()] * 5
        + [_silence_frame()] * 5
    )
    await _push_frames(audio_in, ingest, session, frames)

    await asyncio.sleep(0.25)
    await orch.stop()

    # Core assertion: only one policy call (real end-of-turn, not during pause).
    assert len(policy.calls) == 1, (
        f"expected 1 policy_decision, got {len(policy.calls)} — "
        "SmartTurn veto failed to suppress VAD during thinking pause"
    )

    # Audit trail: vad_signal_suppressed_by_smart_turn event must be logged (invariant #1).
    suppression_events = [e for e in received if e.event_type == "vad_signal_suppressed_by_smart_turn"]
    assert suppression_events, (
        "expected at least one vad_signal_suppressed_by_smart_turn event (audit trail per invariant #1)"
    )
    for evt in suppression_events:
        assert evt.caused_by, f"suppression event {evt.event_id!r} has empty caused_by (orphan — invariant #1)"


@pytest.mark.asyncio
async def test_eou_still_fires_on_real_utterance_end(tmp_path: Path):
    """When SmartTurn says p_done > p_continue (real end), EOU fires normally.

    No thinking-pause suppression should occur; exactly one policy_decision.
    """
    logger, received = _make_logger()
    await logger.start()

    policy = _CapturingPolicy()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-veto-real-end")

    vad_probs = [0.9] * 5 + [0.0] * 5

    # SmartTurn says real end immediately
    smart_model = _ScriptedSmartTurnModel([(0.9, 0.1)])

    orch = _build_orch(
        session_id="test-veto-re",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        smart_turn_model=smart_model,
        speak_policy=policy,
        trace_dir=tmp_path,
    )

    await orch.start()

    frames = [_speech_frame()] * 5 + [_silence_frame()] * 5
    await _push_frames(audio_in, ingest, session, frames)

    await asyncio.sleep(0.25)
    await orch.stop()

    # One policy call expected
    assert len(policy.calls) >= 1, "expected at least one policy_decision on real utterance end"

    # No spurious suppression events
    suppression_events = [e for e in received if e.event_type == "vad_signal_suppressed_by_smart_turn"]
    assert suppression_events == [], (
        f"unexpected vad_signal_suppressed_by_smart_turn when SmartTurn says real end: {suppression_events}"
    )


@pytest.mark.asyncio
async def test_smartturn_veto_overrides_silero_when_in_utterance(tmp_path: Path):
    """SmartTurn veto (p_continue > p_done) prevents VAD from triggering policy even once.

    Non-vacuity: the VAD model itself would have fired (probs cross silence threshold).
    The assertion would fail if the veto were absent (VAD would trigger policy).
    """
    logger, received = _make_logger()
    await logger.start()

    policy = _CapturingPolicy()

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue()
    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-veto-override")

    # VAD crosses silence threshold → would fire without SmartTurn veto
    vad_probs = [0.9] * 5 + [0.0] * 10

    # SmartTurn: only thinking-pause responses (never signals real end)
    smart_model = _ScriptedSmartTurnModel([(0.1, 0.9)])

    orch = _build_orch(
        session_id="test-veto-ov",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        smart_turn_model=smart_model,
        speak_policy=policy,
        trace_dir=tmp_path,
    )

    await orch.start()

    frames = [_speech_frame()] * 5 + [_silence_frame()] * 10
    await _push_frames(audio_in, ingest, session, frames)

    await asyncio.sleep(0.25)
    await orch.stop()

    # With SmartTurn veto in place, policy must NOT have been called.
    # (SmartTurn said thinking pause every time; VAD signal was suppressed.)
    assert len(policy.calls) == 0, (
        f"SmartTurn veto failed: policy was called {len(policy.calls)} time(s) "
        "during a pure thinking-pause window"
    )

    suppression_events = [e for e in received if e.event_type == "vad_signal_suppressed_by_smart_turn"]
    assert suppression_events, "expected vad_signal_suppressed_by_smart_turn event(s) for audit trail"
