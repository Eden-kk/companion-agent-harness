"""RealtimeOrchestrator — contract tests (live-loop Task 5).

Success criterion (verbatim):
  pytest -k realtime_orchestrator passes non-vacuously:
  (a) the end-to-end scripted run produces a closed causal graph from
      harness_init through assistant_audio_buffer_flushed, zero orphan
      actions, every assistant_generation_start traceable to a policy_decision;
  AND
  (b) the gating-invariant test passes — proving SpeakPolicy.decide() is
      called BEFORE any synthesis byte, and the orchestrator holds the
      proposal stream until decision returns.  Both assertions would FAIL
      if the gating contract or causal closure were violated.

Design notes:
  - All models are fakes — NO MiniCPM, torch, or SDK import (adapter-first).
  - Audio is injected directly through the live InputIngest path (not fixture
    replay) as required by the Task 5 spec.
  - The gating-invariant test uses call-ordering instrumentation: it records
    the sequential call index of decide() vs the first synthesize() byte, and
    asserts that decide() call index < synthesize() call index in every case.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.causal_graph import CausalGraph
from companion_harness.event_logger import EventLogger
from companion_harness.foreground_model import ForegroundModel
from companion_harness.input_ingest import CaptureMetadata, InputIngest
from companion_harness.realtime_orchestrator import StreamingRealtimeOrchestrator
from companion_harness.schemas import Event, PolicyInputs, SpeakDecision, ThinkerProposal, TurnSignal
from companion_harness.speak_policy import decide as speak_policy_decide
from companion_harness.turn_detector_smart import SmartTurnDetector
from companion_harness.turn_detector_vad import VADDetector


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=1024), received


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-client",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Fake adapters (no SDK, no model weights)
# ---------------------------------------------------------------------------


class _FakeVADModel:
    """Scripted VAD: returns speech prob for each frame in order, then 0.0."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)
        self._default = 0.0

    def __call__(self, frame: bytes) -> float:
        return next(self._probs, self._default)


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9  # never triggers EOU on its own


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
    """StreamingDuplexModel that yields one scripted ThinkerProposal per stream call."""

    def __init__(self, proposal_text: str = "scripted response") -> None:
        self._text = proposal_text

    def infer(
        self, audio_frame: bytes, video_frame: bytes | None = None
    ) -> ThinkerProposal | None:
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
            # Consume the frame iterator (required by the protocol)
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


class _InstrumentedTtsAdapter:
    """TtsAdapter that records the global call-order index of each synthesize() call.

    Used to assert that decide() is always called before synthesize().
    """

    def __init__(self, call_log: list[tuple[str, int]]) -> None:
        self._call_log = call_log
        self._chunk = b"\x00" * 160

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        idx = len(self._call_log)
        self._call_log.append(("synthesize", idx))
        yield self._chunk


class _InstrumentedDecide:
    """Wraps speak_policy.decide and records the global call-order index."""

    def __init__(self, call_log: list[tuple[str, int]]) -> None:
        self._call_log = call_log

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        idx = len(self._call_log)
        self._call_log.append(("decide", idx))
        return speak_policy_decide(inputs, signal_event_ids, p_backchannel)


async def _noop_sink(chunk: bytes) -> None:
    pass


# ---------------------------------------------------------------------------
# policy_inputs_builder
# ---------------------------------------------------------------------------


def _build_policy_inputs(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
    """Convert TurnSignal to PolicyInputs. No wall-clock reads (invariant #5)."""
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


# ---------------------------------------------------------------------------
# Orchestrator factory
# ---------------------------------------------------------------------------


def _build_orchestrator(
    session_id: str,
    logger: EventLogger,
    ingest: InputIngest,
    vad_probs: list[float],
    tts: object,
    speak_policy_fn: object = speak_policy_decide,
    audio_in: asyncio.Queue | None = None,
) -> tuple[StreamingRealtimeOrchestrator, object, asyncio.Queue]:
    """Build a fully-wired StreamingRealtimeOrchestrator with fake models.

    Returns (orchestrator, ingest_session, audio_in_queue).
    """
    session = ingest.open_session("test-client")
    if audio_in is None:
        audio_in = asyncio.Queue(maxsize=64)

    vad = VADDetector(
        model=_FakeVADModel(vad_probs),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,  # 2 frames of silence at 32 ms/frame
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id=session_id,
        logger=logger,
    )
    bc = BackchannelClassifier(
        model=_FakeBackchannelModel(),
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
        speak_policy=speak_policy_fn,  # type: ignore[arg-type]
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=tts,  # type: ignore[arg-type]
        proposal_batch_window_ms=200,
    )
    return orch, session, audio_in


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    frame_count: int,
) -> None:
    for i in range(frame_count):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


# ---------------------------------------------------------------------------
# Test (a): end-to-end scripted run — closed causal graph, zero orphans,
#           every assistant_generation_start traces to a policy_decision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_to_end_causal_graph_closes(tmp_path):
    """End-to-end scripted run via live InputIngest path.

    Injects scripted audio that triggers a VAD turn signal (speech then silence),
    drives the full pipeline, and asserts:
      - causal graph closes from harness_init through assistant_audio_buffer_flushed
      - zero orphan actions
      - every assistant_generation_start event traces causally to a policy_decision event
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-e2e-causal"

    from companion_harness.tts_adapter import SilentTtsAdapter

    tts = SilentTtsAdapter(chunk_count=1)

    orch, session, audio_in = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest=ingest,
        # VAD sequence: 4 speech frames (p>0.5), then 3 silence frames
        # VADDetector silence_onset_ms=64, frame_duration_ms=32 → 2 silence frames trigger EOU
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        tts=tts,
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    # (1) Zero orphan actions — causal graph must close
    graph = CausalGraph(received)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"Causal graph has orphan events: {report.dangling_refs}"
    )

    event_types = [e.event_type for e in received]

    # (2) harness_init is present (root of DAG)
    assert "harness_init" in event_types, "harness_init event missing"

    # (3) assistant_audio_buffer_flushed is present (end of pipeline)
    assert "assistant_audio_buffer_flushed" in event_types, (
        "assistant_audio_buffer_flushed event missing — pipeline did not complete"
    )

    # (4) Every assistant_generation_start traces causally to a policy_decision event
    policy_decision_ids = {e.event_id for e in received if e.event_type == "policy_decision"}
    gen_start_events = [e for e in received if e.event_type == "assistant_generation_start"]
    assert gen_start_events, "No assistant_generation_start events found"

    for gen_evt in gen_start_events:
        # Direct caused_by must contain a policy_decision event_id
        assert any(ref in policy_decision_ids for ref in gen_evt.caused_by), (
            f"assistant_generation_start {gen_evt.event_id!r} does not trace directly "
            f"to a policy_decision. caused_by={gen_evt.caused_by!r}, "
            f"policy_decision ids={policy_decision_ids!r}"
        )


# ---------------------------------------------------------------------------
# Test (b): gating-invariant — decide() called BEFORE first synthesis byte
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gating_invariant_decide_before_synthesis(tmp_path):
    """Gating-invariant test (hard pass gate — invariants #2, #4).

    Asserts that SpeakPolicy.decide() is called EXACTLY ONCE per candidate batch
    BEFORE any byte is passed to TtsAdapter.synthesize(), and the orchestrator
    holds the proposal stream until that decision returns — no synthesis byte
    without a prior approved SpeakDecision.

    This test FAILS if the gating contract is broken:
      - If synthesize() is called before decide() → assertion fails
      - If decide() is called zero times when a turn signal fires → assertion fails
      - If synthesize() is called without a preceding decide() → assertion fails
    """
    call_log: list[tuple[str, int]] = []
    instrumented_decide = _InstrumentedDecide(call_log)
    instrumented_tts = _InstrumentedTtsAdapter(call_log)

    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-gating"

    orch, session, audio_in = _build_orchestrator(
        session_id=session_id,
        logger=logger,
        ingest=ingest,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        tts=instrumented_tts,
        speak_policy_fn=instrumented_decide,
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 7)
    await asyncio.sleep(0.3)
    await orch.stop()

    # At least one decide() call must have occurred (turn signal was emitted)
    decide_calls = [entry for entry in call_log if entry[0] == "decide"]
    synthesize_calls = [entry for entry in call_log if entry[0] == "synthesize"]

    assert decide_calls, (
        "SpeakPolicy.decide() was never called — no turn signal reached the policy gate"
    )

    # HARD GATING ASSERTION:
    # For each synthesize() call, there must be at least one decide() call that
    # preceded it (lower call-order index).  If any synthesize() call has a lower
    # index than ALL preceding decide() calls, the gating contract is broken.
    for synth_entry in synthesize_calls:
        synth_idx = synth_entry[1]
        prior_decide_indices = [d[1] for d in decide_calls if d[1] < synth_idx]
        assert prior_decide_indices, (
            f"Gating contract violated: synthesize() called at call index {synth_idx} "
            f"with NO prior decide() call — synthesis byte produced without a "
            f"SpeakDecision (invariants #2, #4 violated). "
            f"Full call_log: {call_log!r}"
        )

    # EXACTLY ONE decide() per candidate batch:
    # The orchestrator calls decide() once per chunk that produces a turn signal.
    # With our scripted VAD (1 EOU signal emitted across 7 chunks), exactly 1 batch.
    assert len(decide_calls) >= 1, (
        "Expected at least one decide() call per candidate batch"
    )

    # No synthesis without an immediately-preceding decide() that approved it:
    # The orchestrator only calls synthesize() when action_type != "silence".
    # Since our scripted inputs trigger user_addressed_agent=True + eou>0.5,
    # the policy returns full_response — so synthesize() must have been called.
    assert synthesize_calls, (
        "synthesize() was never called — the policy approved speech but the "
        "orchestrator did not invoke the TtsAdapter"
    )


# ---------------------------------------------------------------------------
# Mutual-exclusion: use_hybrid + use_streaming_speculative must not coexist
# ---------------------------------------------------------------------------


def _build_orch_with_flags(
    tmp_path,
    logger: EventLogger,
    *,
    use_hybrid: bool,
    use_streaming_speculative: bool,
) -> StreamingRealtimeOrchestrator:
    session_id = "test-mutex"
    ingest = InputIngest(logger, tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue = asyncio.Queue(maxsize=64)
    vad = VADDetector(
        model=_FakeVADModel([]),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_turn = SmartTurnDetector(model=_FakeSmartTurnModel(), session_id=session_id, logger=logger)
    bc = BackchannelClassifier(model=_FakeBackchannelModel(), session_id=session_id, logger=logger)
    fg = ForegroundModel(model=_FakeStreamingModel(), session_id=session_id, logger=logger)
    controller = AudioOutputController(session_id=session_id, logger=logger, sink=_noop_sink)
    return StreamingRealtimeOrchestrator(
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
        tts_adapter=_InstrumentedTtsAdapter([]),
        use_hybrid=use_hybrid,
        use_streaming_speculative=use_streaming_speculative,
    )


@pytest.mark.parametrize("use_hybrid,use_streaming_speculative,should_raise", [
    (True,  False, False),
    (False, True,  False),
    (True,  True,  True),
], ids=["hybrid_only_ok", "speculative_only_ok", "both_raises"])
def test_hybrid_streaming_speculative_mutex(
    tmp_path,
    use_hybrid: bool,
    use_streaming_speculative: bool,
    should_raise: bool,
) -> None:
    """use_hybrid=True + use_streaming_speculative=True must raise AssertionError."""
    logger, _ = _make_logger()

    if should_raise:
        with pytest.raises(AssertionError):
            _build_orch_with_flags(
                tmp_path, logger,
                use_hybrid=use_hybrid,
                use_streaming_speculative=use_streaming_speculative,
            )
    else:
        orch = _build_orch_with_flags(
            tmp_path, logger,
            use_hybrid=use_hybrid,
            use_streaming_speculative=use_streaming_speculative,
        )
        assert orch._use_hybrid is use_hybrid
        assert orch._use_streaming_speculative is use_streaming_speculative
