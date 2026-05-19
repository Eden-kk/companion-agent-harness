"""Phase A+C contract tests for Path B listen-transition signal plumbing and drain task.

Success criteria:
  test_on_response_complete_fires_once_per_response — callback fires exactly
    once when is_listen transitions False→True after ≥1 proposal.
  test_on_response_complete_not_fired_without_proposal — callback does NOT fire
    if the model never emits a proposal (stays in listen mode).
  test_snapshot_apis_passthrough — save/restore/has/clear snapshot methods on
    ForegroundModel delegate to underlying model.
  test_drain_task_spawns_on_response_started — drain task spawns and drains ring.
  test_silence_cancels_drain_and_discards_ring — policy=silence cancels drain.
  test_silence_no_active_drain_noop — silence on listen-only turn is a no-op.
  test_drain_finally_preserves_state_on_barge_in_pending — drain finally block
    does not touch state when _drain_state == BARGE_IN_PENDING.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, AsyncGenerator, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator, _DrainState
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import (
    Event,
    PolicyInputs,
    SpeakDecision,
    ThinkerProposal,
    TurnSignal,
)
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


def _make_logger() -> EventLogger:
    async def _sink(evt: Event) -> None:
        pass
    return EventLogger(_sink, maxsize=256)


def _proposal(text: str = "hello") -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="observation",
        content=text,
        trigger="speech",
        confidence=0.9,
        novelty=0.5,
        interruption_cost=0.3,
        max_utterance_ms=5000,
        cooldown_consumed="speech_turn",
        caused_by=["root"],
    )


class _FakeModel:
    """Fake StreamingDuplexModel that drives listen-transitions deterministically.

    script: list of (is_listen, text|None) pairs, one per frame.
    When is_listen=False and text is set, a proposal is emitted.
    """

    def __init__(self, script: list[tuple[bool, str | None]]) -> None:
        self._script = script
        self._last_is_listen: bool = True
        self._on_listen_transition: Callable[[], None] | None = None

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
            idx = 0
            async for _ in frame_iter:
                if idx >= len(self._script):
                    break
                is_listen, text = self._script[idx]
                idx += 1
                prev = self._last_is_listen
                self._last_is_listen = is_listen
                if not prev and is_listen and self._on_listen_transition is not None:
                    self._on_listen_transition()
                if not is_listen and text:
                    yield _proposal(text)
        return _gen()

    async def infer_stream_continuous(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        *,
        on_proposal: Callable[[ThinkerProposal, str], None],
        on_response_complete: Callable[[str], None],
        on_response_started: Callable[[str], None] | None = None,
    ) -> None:
        from uuid import uuid4
        state: dict = {"response_id": None, "proposals": 0, "chars": 0}

        def _on_listen_back() -> None:
            rid = state["response_id"]
            if rid is not None and state["proposals"] > 0:
                on_response_complete(rid)
            state["response_id"] = None
            state["proposals"] = 0
            state["chars"] = 0

        self._on_listen_transition = _on_listen_back
        try:
            gen = await self.infer_stream(frame_iter, caused_by)
            async for proposal in gen:
                if state["response_id"] is None:
                    rid = f"r-{uuid4().hex[:8]}"
                    state["response_id"] = rid
                    if on_response_started is not None:
                        on_response_started(rid)
                state["proposals"] += 1
                state["chars"] += len(proposal.content)
                on_proposal(proposal, state["response_id"])
            _on_listen_back()
        finally:
            self._on_listen_transition = None


async def _frames(n: int) -> AsyncIterator[tuple[bytes, bytes | None]]:
    for _ in range(n):
        yield b"\x00" * 32, None


@pytest.mark.asyncio
async def test_on_response_complete_fires_once_per_response() -> None:
    """Callback fires exactly once for a speak→listen→speak→listen sequence."""
    script = [
        (False, "word1"),   # speak
        (False, "word2"),   # speak
        (True,  None),      # listen — response 1 complete
        (False, "word3"),   # speak — response 2 starts
        (True,  None),      # listen — response 2 complete
    ]
    model = _FakeModel(script)
    completed: list[str] = []

    logger = _make_logger()
    fm = ForegroundModel(model=model, session_id="s1", logger=logger)

    collected: list[ThinkerProposal] = []
    await fm.infer_stream_continuous(
        _frames(5),
        ["root"],
        on_proposal=lambda p, _rid: collected.append(p),
        on_response_complete=completed.append,
    )

    assert len(completed) == 2, f"expected 2 completions, got {len(completed)}: {completed}"
    assert len(set(completed)) == 2, "response_ids must be distinct"
    assert len(collected) == 3


@pytest.mark.asyncio
async def test_on_response_complete_not_fired_without_proposal() -> None:
    """Callback does NOT fire if model stays in listen mode (zero proposals)."""
    script = [
        (True, None),
        (True, None),
        (True, None),
    ]
    model = _FakeModel(script)
    completed: list[str] = []

    logger = _make_logger()
    fm = ForegroundModel(model=model, session_id="s1", logger=logger)

    await fm.infer_stream_continuous(
        _frames(3),
        ["root"],
        on_proposal=lambda p, _rid: None,
        on_response_complete=completed.append,
    )

    assert completed == []


@pytest.mark.asyncio
async def test_snapshot_apis_passthrough() -> None:
    """save/restore/has/clear snapshot methods on ForegroundModel delegate correctly."""
    inner = MagicMock()
    inner.save_speculative_snapshot.return_value = object()
    inner.restore_speculative_snapshot.return_value = True
    inner.has_speculative_snapshot.return_value = False
    inner.clear_speculative_snapshot.return_value = None

    logger = _make_logger()
    fm = ForegroundModel(model=inner, session_id="s2", logger=logger)

    snap = fm.save_speculative_snapshot()
    assert snap is inner.save_speculative_snapshot.return_value
    inner.save_speculative_snapshot.assert_called_once()

    result = fm.restore_speculative_snapshot(caused_by=["evt-1"])
    assert result is True
    inner.restore_speculative_snapshot.assert_called_once_with(caused_by=["evt-1"])

    has = fm.has_speculative_snapshot()
    assert has is False
    inner.has_speculative_snapshot.assert_called_once()

    fm.clear_speculative_snapshot()
    inner.clear_speculative_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_streaming_prefill_text_passthrough() -> None:
    """streaming_prefill_text delegates to underlying model."""
    inner = MagicMock()
    inner.streaming_prefill_text.return_value = None

    logger = _make_logger()
    fm = ForegroundModel(model=inner, session_id="s3", logger=logger)

    fm.streaming_prefill_text(["hello world"], caused_by=["evt-2"])
    inner.streaming_prefill_text.assert_called_once_with(
        text_list=["hello world"], caused_by=["evt-2"]
    )


@pytest.mark.asyncio
async def test_snapshot_apis_noop_on_missing_model() -> None:
    """save no-ops when model lacks the method; restore/has/clear raise AttributeError."""
    inner = MagicMock(spec=[])  # empty spec — no methods
    logger = _make_logger()
    fm = ForegroundModel(model=inner, session_id="s4", logger=logger)

    assert fm.save_speculative_snapshot() is None
    with pytest.raises(AttributeError):
        fm.restore_speculative_snapshot(caused_by=[])
    # has_speculative_snapshot returns False (not AttributeError) when model lacks the method,
    # because orchestrator silence handler uses it as a guard — raising would crash T4.
    assert fm.has_speculative_snapshot() is False
    with pytest.raises(AttributeError):
        fm.clear_speculative_snapshot()
    with pytest.raises(AttributeError):
        fm.streaming_prefill_text(["x"], caused_by=[])


@pytest.mark.asyncio
async def test_ring_entries_tagged_with_response_id() -> None:
    """Phase B: ring entries are 3-tuples with response_id; id changes per response cycle."""
    script = [
        (False, "word1"),   # speak — response A
        (False, "word2"),   # speak — response A
        (True,  None),      # listen — response A complete
        (False, "word3"),   # speak — response B
        (True,  None),      # listen — response B complete
    ]
    model = _FakeModel(script)
    logger = _make_logger()
    fm = ForegroundModel(model=model, session_id="s5", logger=logger)

    ring: list[tuple[int, str, Any]] = []

    def _collect(proposal: Any, response_id: str) -> None:
        ring.append((len(ring), response_id, proposal))

    completed: list[str] = []
    await fm.infer_stream_continuous(
        _frames(5),
        ["root"],
        on_proposal=_collect,
        on_response_complete=completed.append,
    )

    assert len(ring) == 3, f"expected 3 ring entries, got {len(ring)}"

    # Each entry is a 3-tuple.
    for entry in ring:
        assert len(entry) == 3

    # response_id must be non-empty for all entries.
    for _, rid, _ in ring:
        assert rid, f"response_id must not be empty, got {rid!r}"

    # First two proposals share response A; third is response B.
    rid_a = ring[0][1]
    assert ring[1][1] == rid_a, "word1 and word2 should share the same response_id"
    rid_b = ring[2][1]
    assert rid_b != rid_a, "response B must have a different response_id than response A"

    # completed response_ids match what was tagged in the ring.
    assert set(completed) == {rid_a, rid_b}


# ---------------------------------------------------------------------------
# Phase C: drain-until-listen state machine tests
# ---------------------------------------------------------------------------


def _make_logger_c() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def _sink(evt: Event) -> None:
        received.append(evt)

    return EventLogger(_sink, maxsize=4096), received


def _meta_c(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-phase-c",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


class _StreamingTtsStub:
    """TTS adapter with synthesize_streaming support."""

    def __init__(self) -> None:
        self.synthesize_streaming_calls: list[str] = []

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        yield b"\x00" * 160

    async def synthesize_streaming(
        self, text_chunks: AsyncIterator[str], prosody_tags: list[str]
    ) -> AsyncIterator[bytes]:
        collected = []
        async for chunk in text_chunks:
            collected.append(chunk)
        self.synthesize_streaming_calls.append("".join(collected))
        yield b"\x00" * 160


async def _noop_sink_c(chunk: bytes) -> None:
    pass


class _SilencePolicyC:
    def __call__(
        self, inputs: PolicyInputs, signal_event_ids: list[str], p_backchannel: float = 0.0
    ) -> SpeakDecision:
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


class _FullResponsePolicyC:
    def __call__(
        self, inputs: PolicyInputs, signal_event_ids: list[str], p_backchannel: float = 0.0
    ) -> SpeakDecision:
        return SpeakDecision(
            action_type="full_response",
            primary_reason_code=ReasonCode.USER_ADDRESSED_AGENT,
            supporting_reason_codes=[],
            redacted_explanation=None,
            caused_by=list(signal_event_ids),
            budget_bucket=None,
            allowed_prosody_tags=[],
            max_duration_ms=None,
        )


class _RespondingModel:
    """Model that calls on_response_started + on_response_complete via the callbacks."""

    def __init__(self, response_text: str = "hello world", trigger_after: int = 2) -> None:
        self._text = response_text
        self._trigger_after = trigger_after

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
            yield  # noqa
        return _gen()

    async def infer_stream_continuous(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
        *,
        on_proposal: Callable[[ThinkerProposal, str], None],
        on_response_complete: Callable[[str], None],
        on_response_started: Callable[[str], None] | None = None,
    ) -> None:
        from uuid import uuid4
        frame_count = 0
        rid = f"r-{uuid4().hex[:8]}"
        started = False
        async for _ in frame_iter:
            frame_count += 1
            if frame_count == self._trigger_after and not started:
                started = True
                if on_response_started is not None:
                    on_response_started(rid)
                p = ThinkerProposal(
                    proposal_type="observation",
                    content=self._text,
                    trigger="speech",
                    confidence=0.9,
                    novelty=0.5,
                    interruption_cost=0.3,
                    max_utterance_ms=5000,
                    cooldown_consumed="speech_turn",
                    caused_by=list(caused_by),
                )
                on_proposal(p, rid)
            if frame_count == self._trigger_after + 2 and started:
                on_response_complete(rid)
                started = False


def _build_orch_c(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session: Any,
    audio_in: asyncio.Queue,
    model: Any,
    speak_policy: Any,
    tts_adapter: Any = None,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=lambda _: 0.9,
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=lambda _: (0.9, 0.1),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=lambda _: 0.0,
        session_id=session_id,
        logger=logger,
    )
    fg = ForegroundModel(model=model, session_id=session_id, logger=logger)
    controller = AudioOutputController(
        session_id=session_id, logger=logger, sink=_noop_sink_c
    )
    _tts = tts_adapter or _StreamingTtsStub()
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=lambda sig, hist: PolicyInputs(
            user_speaking=True,
            eou_probability=sig.p_done,
            assistant_speaking=False,
            scene_change_score=0.0,
            deictic_reference=False,
            user_addressed_agent=True,
            urgency_score=0.0,
            proactivity_budget_remaining={},
            privacy_mode="normal",
            current_task_mode="normal",
            social_mode="user_addressing_agent",
            risk_mode="normal",
            cooldown_state={},
            attachment_risk_level=0.0,
            audio_visual_conflict_score=0.0,
            grounding_confidence=1.0,
            deictic_ambiguous=False,
        ),
        speak_policy=speak_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=_tts,
        use_streaming_speculative=True,
    )


@pytest.mark.asyncio
async def test_drain_task_spawns_on_response_started(tmp_path: Path) -> None:
    """Drain task spawns when on_response_started fires; ring entries are drained."""
    logger, received = _make_logger_c()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("phase-c-drain")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    model = _RespondingModel(response_text="drain me", trigger_after=2)
    orch = _build_orch_c(
        session_id="pc-drain",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_FullResponsePolicyC(),
    )

    await orch.start()

    import struct
    speech = struct.pack("<h", 4000) * 256
    silence = b"\x00" * 512
    frames = [speech] * 5 + [silence] * 12
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta_c(i))
        await audio_in.put((frame, evt.event_id))

    await asyncio.sleep(0.4)
    await orch.stop()

    # At least one drain_response_complete or path_b_eou_policy_approved event emitted.
    drain_evts = [
        e for e in received
        if e.event_type in ("drain_response_complete", "path_b_eou_policy_approved")
    ]
    assert len(drain_evts) >= 1, f"expected drain events, got event types: {[e.event_type for e in received]}"

    # Drain state must be IDLE after completion.
    assert orch._drain_state == _DrainState.IDLE


@pytest.mark.asyncio
async def test_silence_cancels_drain_and_discards_ring(tmp_path: Path) -> None:
    """policy=silence + active drain task → cancel + ring discard + response_suppressed_by_policy."""
    logger, received = _make_logger_c()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("phase-c-silence")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    model = _RespondingModel(response_text="suppress me", trigger_after=2)
    orch = _build_orch_c(
        session_id="pc-silence",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_SilencePolicyC(),
    )

    await orch.start()

    import struct
    speech = struct.pack("<h", 4000) * 256
    silence = b"\x00" * 512
    frames = [speech] * 5 + [silence] * 12
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta_c(i))
        await audio_in.put((frame, evt.event_id))

    await asyncio.sleep(0.4)
    await orch.stop()

    suppressed = [e for e in received if e.event_type == "response_suppressed_by_policy"]
    assert len(suppressed) >= 1, "expected response_suppressed_by_policy event"

    # Drain state must return to IDLE.
    assert orch._drain_state == _DrainState.IDLE
    assert orch._active_drain_task is None


@pytest.mark.asyncio
async def test_silence_no_active_drain_noop(tmp_path: Path) -> None:
    """policy=silence with no active drain (listen-only turn) → no crash, IDLE state."""
    logger, received = _make_logger_c()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("phase-c-silence-noop")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    # Model that never calls on_response_started (listen-only).
    class _ListenOnlyModel:
        def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
            return None

        def set_context(self, items: Any) -> None:
            pass

        async def infer_stream(self, frame_iter, caused_by, context_items=()):  # type: ignore
            async def _g():
                async for _ in frame_iter:
                    pass
                return
                yield  # noqa
            return _g()

        async def infer_stream_continuous(self, frame_iter, caused_by, *, on_proposal, on_response_complete, on_response_started=None):  # type: ignore
            async for _ in frame_iter:
                pass

    model = _ListenOnlyModel()
    orch = _build_orch_c(
        session_id="pc-silence-noop",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_SilencePolicyC(),
    )

    await orch.start()

    import struct
    speech = struct.pack("<h", 4000) * 256
    silence = b"\x00" * 512
    frames = [speech] * 5 + [silence] * 12
    for i, frame in enumerate(frames):
        evt = ingest.ingest_chunk(session, frame, _meta_c(i))
        await audio_in.put((frame, evt.event_id))

    await asyncio.sleep(0.4)
    await orch.stop()

    # No crash; drain state stays IDLE; no active task.
    assert orch._drain_state == _DrainState.IDLE
    assert orch._active_drain_task is None

    # response_suppressed_by_policy still emitted (silence handler ran).
    suppressed = [e for e in received if e.event_type == "response_suppressed_by_policy"]
    assert len(suppressed) >= 1


@pytest.mark.asyncio
async def test_drain_finally_preserves_state_on_barge_in_pending() -> None:
    """Drain task finally block does NOT clear state when _drain_state == BARGE_IN_PENDING."""
    logger, _ = _make_logger_c()
    await logger.start()

    # Build a minimal orchestrator and manually inject BARGE_IN_PENDING state,
    # then cancel the drain task and verify finally block respects the guard.
    ingest = InputIngest(logger=logger, blob_dir=Path("/tmp"))
    session = ingest.open_session("phase-c-barge-guard")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    model = _RespondingModel(response_text="barge guard", trigger_after=1)
    orch = _build_orch_c(
        session_id="pc-barge-guard",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_FullResponsePolicyC(),
    )

    await orch.start()

    # Manually spawn a drain task and inject BARGE_IN_PENDING before cancellation.
    rid = "r-test-guard"
    orch._active_response_id = rid
    orch._drain_state = _DrainState.DRAINING
    # Pre-register the complete event (never fires — task will be cancelled).
    _never_complete = asyncio.Event()
    orch._drain_complete_callbacks[rid] = lambda _: None

    drain_task = asyncio.get_running_loop().create_task(
        orch._drain_response_task(rid, _never_complete),
        name=f"drain-{rid}",
    )
    orch._active_drain_task = drain_task

    # Transition to BARGE_IN_PENDING before cancelling.
    orch._drain_state = _DrainState.BARGE_IN_PENDING
    drain_task.cancel()
    try:
        await drain_task
    except (asyncio.CancelledError, Exception):
        pass

    # The finally block must NOT have reset state to IDLE since BARGE_IN_PENDING was set.
    assert orch._drain_state == _DrainState.BARGE_IN_PENDING, (
        f"drain finally block must not reset state when BARGE_IN_PENDING, got {orch._drain_state}"
    )
    assert orch._active_drain_task is drain_task, (
        "drain finally block must not clear _active_drain_task when BARGE_IN_PENDING"
    )

    await orch.stop()


# ---------------------------------------------------------------------------
# Phase D: barge-in handler (snapshot-only per plan §3.5 + A.5 fallback)
# ---------------------------------------------------------------------------


def _build_orch_path_b(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session: Any,
    audio_in: asyncio.Queue,
    inner: Any,
) -> StreamingRealtimeOrchestrator:
    fg = ForegroundModel(model=inner, session_id=session_id, logger=logger)
    vad = VADDetector(model=lambda _: 0.9, session_id=session_id, logger=logger,
                      speech_threshold=0.5, silence_onset_ms=64, frame_duration_ms=32)
    smart_turn = SmartTurnDetector(model=lambda _: (0.9, 0.1), session_id=session_id, logger=logger)
    bc = BackchannelClassifier(model=lambda _: 0.0, session_id=session_id, logger=logger)
    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink_c)
    return StreamingRealtimeOrchestrator(
        session_id=session_id,
        logger=logger,
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=lambda sig, hist: PolicyInputs(
            user_speaking=True, eou_probability=sig.p_done, assistant_speaking=False,
            scene_change_score=0.0, deictic_reference=False, user_addressed_agent=True,
            urgency_score=0.0, proactivity_budget_remaining={}, privacy_mode="normal",
            current_task_mode="normal", social_mode="user_addressing_agent",
            risk_mode="normal", cooldown_state={}, attachment_risk_level=0.0,
            audio_visual_conflict_score=0.0, grounding_confidence=1.0, deictic_ambiguous=False,
        ),
        speak_policy=_FullResponsePolicyC(),
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=_StreamingTtsStub(),
        use_streaming_speculative=True,
    )


@pytest.mark.asyncio
async def test_barge_in_during_active_drain_cancels_and_restores(tmp_path: Path) -> None:
    """Barge-in fires during active drain → drain cancelled, ring discarded, snapshot restored."""
    logger, received = _make_logger_c()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("phase-d-barge-active")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    inner = MagicMock()
    inner.save_speculative_snapshot.return_value = object()
    inner.restore_speculative_snapshot.return_value = True
    inner.has_speculative_snapshot.return_value = True
    inner.infer.return_value = None
    inner.set_context.return_value = None

    async def _isc(frame_iter, caused_by, *, on_proposal, on_response_complete, on_response_started=None):  # type: ignore
        async for _ in frame_iter:
            pass

    inner.infer_stream_continuous = _isc

    orch = _build_orch_path_b(
        session_id="pd-active",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        inner=inner,
    )

    rid = "r-phase-d-test"
    orch._active_response_id = rid
    orch._drain_state = _DrainState.DRAINING
    orch._proposal_ring = [
        (0, rid, _proposal("hello")),
        (1, rid, _proposal("world")),
        (2, "other-rid", _proposal("keep")),
    ]

    never_done = asyncio.Event()
    drain_task = asyncio.get_running_loop().create_task(
        asyncio.wait_for(never_done.wait(), timeout=60.0),
        name="fake-drain",
    )
    orch._active_drain_task = drain_task
    orch._barge_in_in_flight = True

    await orch._fire_barge_in("onset-evt-1")

    assert drain_task.done()
    assert all(r != rid for (_, r, _) in orch._proposal_ring), "ring entries for active rid must be discarded"
    assert len(orch._proposal_ring) == 1
    inner.restore_speculative_snapshot.assert_called_once_with(caused_by=["onset-evt-1"])
    assert orch._active_drain_task is None
    assert orch._active_response_id is None
    assert orch._drain_state == _DrainState.IDLE
    assert not orch._barge_in_in_flight
    await asyncio.sleep(0.05)
    assert any(e.event_type == "path_b_barge_in" for e in received), \
        f"expected path_b_barge_in event, got {[e.event_type for e in received]}"

    await orch.stop()


@pytest.mark.asyncio
async def test_barge_in_no_active_drain_falls_through(tmp_path: Path) -> None:
    """Path-B mode ON (use_streaming_speculative=True) + _active_drain_task=None → Path-B guard skipped, falls through to hybrid no-op path."""
    logger, received = _make_logger_c()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("phase-d-fallthrough")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    model = _RespondingModel(response_text="noop", trigger_after=99)
    orch = _build_orch_c(
        session_id="pd-fallthrough",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        model=model,
        speak_policy=_FullResponsePolicyC(),
    )

    assert orch._active_drain_task is None
    orch._barge_in_in_flight = True

    await orch._fire_barge_in("onset-evt-2")

    await asyncio.sleep(0.05)
    assert not any(e.event_type == "path_b_barge_in" for e in received), \
        "path_b_barge_in must not fire when no active drain task"
    assert any(e.event_type == "barge_in_trigger_no_op" for e in received), \
        f"expected barge_in_trigger_no_op, got {[e.event_type for e in received]}"
    assert not orch._barge_in_in_flight

    await orch.stop()


@pytest.mark.asyncio
async def test_barge_in_no_snapshot_skips_restore(tmp_path: Path) -> None:
    """Barge-in during drain when has_speculative_snapshot() is False → no restore, state cleared."""
    logger, received = _make_logger_c()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("phase-d-no-snap")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    inner = MagicMock()
    inner.has_speculative_snapshot.return_value = False
    inner.restore_speculative_snapshot.return_value = True
    inner.infer.return_value = None
    inner.set_context.return_value = None

    async def _isc(frame_iter, caused_by, *, on_proposal, on_response_complete, on_response_started=None):  # type: ignore
        async for _ in frame_iter:
            pass

    inner.infer_stream_continuous = _isc

    orch = _build_orch_path_b(
        session_id="pd-nosnap",
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        inner=inner,
    )

    rid = "r-nosnap"
    orch._active_response_id = rid
    orch._drain_state = _DrainState.DRAINING
    orch._proposal_ring = [(0, rid, _proposal("snap me"))]

    never_done = asyncio.Event()
    drain_task = asyncio.get_running_loop().create_task(
        asyncio.wait_for(never_done.wait(), timeout=60.0),
        name="fake-drain-nosnap",
    )
    orch._active_drain_task = drain_task
    orch._barge_in_in_flight = True

    await orch._fire_barge_in("onset-evt-3")

    inner.restore_speculative_snapshot.assert_not_called()
    assert orch._active_drain_task is None
    assert orch._active_response_id is None
    assert orch._drain_state == _DrainState.IDLE
    assert not orch._barge_in_in_flight

    await orch.stop()
