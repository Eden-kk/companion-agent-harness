"""StreamingRealtimeOrchestrator — contract tests (live-loop Task 5b).

Success criterion (verbatim):
  pytest -k realtime_orchestrator_streaming passes non-vacuously.
  All 7 tests pass. The gating-invariant test fails if a synthesis byte precedes
  decide(). The coalescing test fails if a second EOU within the grace window
  produces a second decision_future. The decide()-raise test fails if the
  orchestrator crashes instead of falling back to silence.

Design notes:
  - All models are fakes — NO MiniCPM, torch, or SDK import (adapter-first).
  - Tests drive the orchestrator via audio_in queue; the caller is responsible
    for calling InputIngest.ingest_chunk() and pushing event_ids.
  - The gating-invariant test records time.monotonic_ns() at decide() and
    synthesize() call sites and asserts decide-ts < synthesize-ts.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator

import pytest

from companion_harness.audio_output_controller import AudioOutputController
from companion_harness.backchannel_classifier import BackchannelClassifier
from companion_harness.causal_graph import CausalGraph
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
# Shared helpers
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


def _pcm_speech(n_bytes: int = 512) -> bytes:
    # Non-zero bytes so SmartTurnDetector's RMS check sees speech energy.
    return b"\x10" * n_bytes


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-streaming",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


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
        return 0.1, 0.9  # never triggers EOU


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
    """StreamingDuplexModel that yields scripted proposals per batch."""

    def __init__(self, proposal_text: str = "scripted response") -> None:
        self._text = proposal_text

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


class _FakeStreamingModelDelayed:
    """StreamingDuplexModel that yields proposals after a short async delay.

    Used in the multi-turn regression test: the delay ensures proposal_buffer
    is empty when T4 first checks it, forcing the _first_proposal_event.wait()
    path — which is exactly where the turn-N+1 silent bug lives.
    """

    def __init__(self, delay_s: float = 0.02, proposal_text: str = "scripted response") -> None:
        self._delay_s = delay_s
        self._text = proposal_text

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
            await asyncio.sleep(self._delay_s)
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


class _FakeStreamingModelNoProposals:
    """StreamingDuplexModel that never yields any proposals."""

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
            return
            yield  # make it a generator

        return _gen()


class _SpyTtsAdapter:
    """TtsAdapter that records monotonic-ns timestamps of synthesize() calls."""

    def __init__(self) -> None:
        self.synthesize_call_timestamps_ns: list[int] = []
        self._chunk = b"\x00" * 160

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        self.synthesize_call_timestamps_ns.append(time.monotonic_ns())
        yield self._chunk

    @property
    def synthesize_call_count(self) -> int:
        return len(self.synthesize_call_timestamps_ns)


class _SpySpeakPolicy:
    """Wraps speak_policy.decide; records monotonic-ns timestamps per call."""

    def __init__(self, delegate=_real_decide) -> None:
        self._delegate = delegate
        self.call_timestamps_ns: list[int] = []

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        self.call_timestamps_ns.append(time.monotonic_ns())
        return self._delegate(inputs, signal_event_ids, p_backchannel)

    @property
    def call_count(self) -> int:
        return len(self.call_timestamps_ns)


class _AlwaysSilencePolicy:
    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
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


class _RaisingPolicy:
    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        raise RuntimeError("scripted policy failure")


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
# Orchestrator builder
# ---------------------------------------------------------------------------


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    streaming_model=None,
    speak_policy=None,
    tts_adapter=None,
    proposal_batch_window_ms: int = 200,
) -> StreamingRealtimeOrchestrator:
    vad = VADDetector(
        model=_FakeVADModel(vad_probs),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
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
        model=streaming_model or _FakeStreamingModel(),
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
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=speak_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=tts_adapter or SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=proposal_batch_window_ms,
    )


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    frame_count: int,
) -> None:
    """Push `frame_count` silent audio frames to audio_in via ingest_chunk."""
    for i in range(frame_count):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((evt.payload_ref and _pcm_chunk() or _pcm_chunk(), evt.event_id))


async def _push_scripted_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    vad_probs: list[float],
) -> None:
    """Push frames matching the VAD script (speech vs. silence bytes don't affect fakes)."""
    for i, _ in enumerate(vad_probs):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


# ---------------------------------------------------------------------------
# Test 1: End-to-end causal-graph closure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_closes_causal_graph_end_to_end(tmp_path: Path):
    """Scripted audio drives the streaming loop; closed DAG from harness_init
    through assistant_audio_buffer_flushed, zero orphans, every
    assistant_generation_start traces back to policy_decision."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-e2e"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
    )

    await orch.start()
    await _push_scripted_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])

    # Let the event loop process all queued tasks.
    await asyncio.sleep(0.3)
    await orch.stop()

    # Causal graph must close.
    graph = CausalGraph(received)
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"Causal graph has orphan events: {report.dangling_refs}"
    )

    event_types = [e.event_type for e in received]
    assert "harness_init" in event_types
    assert "assistant_audio_buffer_flushed" in event_types, (
        "assistant_audio_buffer_flushed missing — pipeline did not complete"
    )

    # Every assistant_generation_start must trace directly to a policy_decision event.
    policy_decision_ids = {e.event_id for e in received if e.event_type == "policy_decision"}
    gen_evts = [e for e in received if e.event_type == "assistant_generation_start"]
    assert gen_evts, "No assistant_generation_start events found"
    for gen_evt in gen_evts:
        assert any(ref in policy_decision_ids for ref in gen_evt.caused_by), (
            f"assistant_generation_start {gen_evt.event_id!r} does not trace to a "
            f"policy_decision. caused_by={gen_evt.caused_by!r}"
        )


# ---------------------------------------------------------------------------
# Test 2: Gating-contract invariant — decide() called BEFORE first synthesize()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gating_invariant_decide_before_synthesis(tmp_path: Path):
    """THE HEADLINE GATING-INVARIANT TEST.

    Asserts SpeakPolicy.decide() call timestamp < TtsAdapter.synthesize()
    call timestamp for every batch.  FAILS if a byte reaches TtsAdapter
    before decide() is called (invariants #2, #4).

    Verbatim assertion:
        assert spy_decide.call_timestamps_ns[0] < spy_tts.synthesize_call_timestamps_ns[0], (
            f"SpeakPolicy.decide() must be called BEFORE TtsAdapter.synthesize(). "
            f"decide @ {spy_decide.call_timestamps_ns[0]} ns, "
            f"synthesize @ {spy_tts.synthesize_call_timestamps_ns[0]} ns "
            f"(invariants #2, #4)"
        )
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-gating"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy_decide = _SpySpeakPolicy()
    spy_tts = _SpyTtsAdapter()

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        speak_policy=spy_decide,
        tts_adapter=spy_tts,
    )

    await orch.start()
    await _push_scripted_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    await asyncio.sleep(0.3)
    await orch.stop()

    # At least one decide() call and one synthesize() call (VAD EOU → full_response).
    assert spy_decide.call_count >= 1, "SpeakPolicy.decide() was never called"
    assert spy_tts.synthesize_call_count >= 1, "TtsAdapter.synthesize() was never called"

    # Timestamps from the FIRST speech batch.
    # Find first synthesize that was preceded by at least one decide.
    first_synth_ts = spy_tts.synthesize_call_timestamps_ns[0]
    # There must be a decide() call strictly before the first synthesize().
    decide_before_synth = [ts for ts in spy_decide.call_timestamps_ns if ts < first_synth_ts]
    assert decide_before_synth, (
        f"Gating contract violated: no SpeakPolicy.decide() call preceded first "
        f"TtsAdapter.synthesize() (invariants #2, #4).\n"
        f"decide timestamps (ns): {spy_decide.call_timestamps_ns}\n"
        f"synthesize timestamps (ns): {spy_tts.synthesize_call_timestamps_ns}"
    )

    # Verbatim assertion from plan §7 Test 2.
    assert spy_decide.call_timestamps_ns[0] < spy_tts.synthesize_call_timestamps_ns[0], (
        f"SpeakPolicy.decide() must be called BEFORE TtsAdapter.synthesize(). "
        f"decide @ {spy_decide.call_timestamps_ns[0]} ns, "
        f"synthesize @ {spy_tts.synthesize_call_timestamps_ns[0]} ns "
        f"(invariants #2, #4)"
    )

    # assistant_generation_start must trace to policy_decision.
    gen_evts = [e for e in received if e.event_type == "assistant_generation_start"]
    policy_decision_ids = {e.event_id for e in received if e.event_type == "policy_decision"}
    assert gen_evts, "No assistant_generation_start events"
    assert any(e.event_type == "policy_decision" and e.event_id in gen_evts[0].caused_by for e in received), (
        "assistant_generation_start does not trace to policy_decision"
    )


# ---------------------------------------------------------------------------
# Test 3: Silence decision produces no synthesis
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_silence_decision_produces_no_synthesis(tmp_path: Path):
    """Always-silence policy: zero synthesize() calls, zero assistant_generation_start,
    but policy_decision with action_type='silence' IS logged."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-silence"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    silence_policy = _AlwaysSilencePolicy()
    spy_tts = _SpyTtsAdapter()

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        speak_policy=silence_policy,
        tts_adapter=spy_tts,
    )

    await orch.start()
    await _push_scripted_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    await asyncio.sleep(0.3)
    await orch.stop()

    assert spy_tts.synthesize_call_count == 0, (
        f"TtsAdapter.synthesize() was called {spy_tts.synthesize_call_count} times "
        "despite always-silence policy"
    )
    gen_evts = [e for e in received if e.event_type == "assistant_generation_start"]
    assert len(gen_evts) == 0, (
        f"assistant_generation_start emitted with always-silence policy: {gen_evts}"
    )
    silence_decisions = [
        e for e in received
        if e.event_type == "policy_decision"
    ]
    assert silence_decisions, "No policy_decision events logged despite always-silence policy"


# ---------------------------------------------------------------------------
# Test 4: Edge case (h) — grace window expires when no proposals arrive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grace_window_emits_synthesis_skipped_no_proposal(tmp_path: Path):
    """When foreground model yields no proposals and grace window expires,
    synthesis_skipped_no_proposal event is emitted and no synthesis occurs."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-grace"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        streaming_model=_FakeStreamingModelNoProposals(),
        proposal_batch_window_ms=50,  # short window so test completes fast
    )

    spy_tts = _SpyTtsAdapter()
    # Re-build with spy_tts (rebuild needed to inject no-proposals model + spy_tts).
    vad = VADDetector(
        model=_FakeVADModel([0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
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
        model=_FakeStreamingModelNoProposals(),
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
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=spy_tts,
        proposal_batch_window_ms=50,
    )

    await orch.start()
    await _push_scripted_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    # Wait longer than grace window (50ms) to let it expire.
    await asyncio.sleep(0.5)
    await orch.stop()

    skipped = [e for e in received if e.event_type == "synthesis_skipped_no_proposal"]
    assert skipped, "synthesis_skipped_no_proposal event not emitted despite no proposals"
    assert spy_tts.synthesize_call_count == 0, (
        "TtsAdapter.synthesize() called despite no proposals"
    )


# ---------------------------------------------------------------------------
# Test 5: Edge case (i) — coalescing rapid EOUs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coalescing_rapid_eou_signals(tmp_path: Path):
    """Two EOU TurnSignals fired while decision_future is pending (unresolved)
    — assert only ONE decision_future is processed and one turn_signal_coalesced
    event is emitted."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-coalesce"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[],
    )
    await orch.start()

    # Set _decision_in_flight to True to simulate a decision already in-flight.
    # (Pre-fix this test manipulated _pending_decision_future directly; after the
    # fix the coalescing guard uses _decision_in_flight instead — see Finding 9.)
    orch._decision_in_flight = True
    orch._pending_signal_evt_id = "fake-evt-id-1"

    # Create a fake TurnSignal for the second signal.
    second_signal = TurnSignal(
        detector="vad",
        p_done=0.9,
        p_continue=0.1,
        p_backchannel=0.0,
        confidence=0.9,
        evidence_event_ids=["fake-evt-id-2"],
    )
    await orch._t2_inbox.put((second_signal, "fake-evt-id-2"))

    # Let T2 process the signal.
    await asyncio.sleep(0.1)

    await orch.stop()

    coalesced = [e for e in received if e.event_type == "turn_signal_coalesced"]
    assert len(coalesced) >= 1, (
        "turn_signal_coalesced event not emitted when second EOU arrived with "
        "_decision_in_flight=True"
    )

    # Assert only ONE decision_future was processed for this scenario
    # (the pending one was already there; the second was coalesced).
    policy_decisions = [e for e in received if e.event_type == "policy_decision"]
    # No new policy_decision should have been created for the coalesced signal.
    assert len(policy_decisions) == 0, (
        f"A new policy_decision was created for the coalesced signal: {policy_decisions}"
    )


# ---------------------------------------------------------------------------
# Test 6: Edge case (j) — decide() raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_raises_falls_back_to_silence(tmp_path: Path):
    """When speak_policy.decide() raises, the orchestrator MUST NOT crash.
    Fallback silence decision is logged; policy_decision_error event emitted."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-raise"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)
    spy_tts = _SpyTtsAdapter()

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=[0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1],
        speak_policy=_RaisingPolicy(),
        tts_adapter=spy_tts,
    )

    await orch.start()
    await _push_scripted_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    await asyncio.sleep(0.3)

    # Orchestrator must still be alive (not crashed).
    running_tasks = [t for t in orch._tasks if not t.done()]
    assert running_tasks, "Orchestrator tasks died after decide() raised — should have survived"

    await orch.stop()

    # policy_decision_error must be emitted.
    error_evts = [e for e in received if e.event_type == "policy_decision_error"]
    assert error_evts, "policy_decision_error event not emitted after decide() raised"

    # policy_decision with fallback silence must be logged.
    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    assert policy_evts, "No policy_decision event logged after decide() raised"

    # No synthesis should have occurred (fallback is silence).
    assert spy_tts.synthesize_call_count == 0, (
        "TtsAdapter.synthesize() called after decide() raised — expected silence fallback"
    )


# ---------------------------------------------------------------------------
# Test 7: Determinism boundary — signal_history sorted by evidence_event_ids[0]
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_determinism_boundary_signal_history_sorted(tmp_path: Path):
    """signal_history passed to policy_inputs_builder is sorted by
    evidence_event_ids[0] lexicographic — wall-clock never enters PolicyInputs."""
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-determinism"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    # Spy policy_inputs_builder records the sorted_history passed to it.
    captured_histories: list[list[TurnSignal]] = []

    def spy_builder(signal: TurnSignal, signal_history: list[TurnSignal]) -> PolicyInputs:
        captured_histories.append(list(signal_history))
        return _build_policy_inputs(signal, signal_history)

    vad = VADDetector(
        model=_FakeVADModel([0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
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
        policy_inputs_builder=spy_builder,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
    )

    await orch.start()
    await _push_scripted_frames(audio_in, ingest, session, [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1])
    await asyncio.sleep(0.3)
    await orch.stop()

    assert captured_histories, "policy_inputs_builder was never called"

    for history in captured_histories:
        if len(history) < 2:
            continue
        event_ids = [s.evidence_event_ids[0] if s.evidence_event_ids else "" for s in history]
        assert event_ids == sorted(event_ids), (
            f"signal_history is NOT sorted by evidence_event_ids[0] lexicographic. "
            f"Got: {event_ids}"
        )


# ---------------------------------------------------------------------------
# Test 8: Multi-turn regression — _first_proposal_event + proposal_buffer cleared at batch_open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_turn_stale_state_cleared_at_batch_open(tmp_path: Path):
    """Regression test for the turn-N+1 silent bug.

    Root cause: _first_proposal_event (asyncio.Event) was never cleared between
    turns at batch_open (realtime_orchestrator.py:305 init, :1041-1042, :1316-1348).
    A stale SET event from turn N caused turn N+1's T4 to return instantly from
    _first_proposal_event.wait(), then snapshot a stale or empty proposal_buffer.

    Fix: _first_proposal_event.clear() + proposal_buffer.clear() at batch_open
    (realtime_orchestrator.py:1041-1042) before _batch_open_event.set().

    Test design: after turn 1 completes, forcibly re-set _first_proposal_event and
    inject a stale proposal into proposal_buffer (simulating the race where T3 of
    turn N yields a second proposal after T4's snapshot-clear at line 1347-1348).
    Then trigger turn 2.

    WITHOUT fix: T2's batch_open does NOT clear state, T4 for turn 2 finds the
    stale event immediately set and snaps the stale "turn-1-stale" proposal.
    The spy TTS records "turn-1-stale" as the text for turn 2.

    WITH fix: T2 clears both before waking T3, stale state is gone, T4 blocks
    until T3 yields the real "scripted response" proposal for turn 2.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-multiturn"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)

    # Spy TTS records the text passed to synthesize() for each call.
    synthesized_texts: list[str] = []

    class _RecordingTtsAdapter:
        async def synthesize(self, text: str, prosody_tags: list[str]):
            synthesized_texts.append(text)
            yield b"\x00" * 160

    # One turn = 2 speech frames + 2 silence frames (silence_onset_ms=64, frame_duration_ms=32).
    one_turn = [0.9, 0.9, 0.1, 0.1]
    vad_probs = one_turn * 2  # 2 turns worth of VAD probs

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        streaming_model=_FakeStreamingModelDelayed(delay_s=0.01),
        tts_adapter=_RecordingTtsAdapter(),
        proposal_batch_window_ms=200,
    )

    await orch.start()

    # Push turn 1 and wait for it to fully complete.
    frame_idx = 0
    for _ in one_turn:
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(frame_idx))
        await audio_in.put((_pcm_chunk(), evt.event_id))
        frame_idx += 1
    await asyncio.sleep(0.4)  # wait for turn 1 TTS to complete + _decision_in_flight reset

    # Simulate the race: T3 of turn 1 yielded a SECOND proposal after T4's snapshot,
    # leaving stale state that the fix must clear at the next batch_open.
    stale_proposal = ThinkerProposal(
        proposal_type="observation",
        content="turn-1-stale",
        trigger="eou",
        confidence=0.9,
        novelty=0.5,
        interruption_cost=0.1,
        max_utterance_ms=2000,
        cooldown_consumed="full_response",
        caused_by=[],
    )
    orch.proposal_buffer.append(stale_proposal)
    orch._first_proposal_event.set()

    # Push turn 2.
    for _ in one_turn:
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(frame_idx))
        await audio_in.put((_pcm_chunk(), evt.event_id))
        frame_idx += 1
    await asyncio.sleep(0.4)  # wait for turn 2 TTS to complete

    await orch.stop()

    assert len(synthesized_texts) >= 2, (
        f"Expected TTS for at least 2 turns, got {synthesized_texts}. "
        "Turn-N+1 silent regression."
    )
    assert synthesized_texts[1] != "turn-1-stale", (
        f"Turn 2 TTS used stale content from turn 1: {synthesized_texts[1]!r}. "
        "Fix must clear proposal_buffer at batch_open (realtime_orchestrator.py:1041-1042)."
    )


@pytest.mark.asyncio
async def test_multi_turn_tts_fires_on_each_turn(tmp_path: Path):
    """Behavioral regression: TTS fires on each independent turn (turn-N+1 silent bug).

    Root cause: realtime_orchestrator.py:305 + :1041-1042 + :1316-1348.
    Fix: clear _first_proposal_event + proposal_buffer at batch_open.
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-streaming-multiturn-behavioral"
    session = ingest.open_session("test-client")

    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=256)
    spy_tts = _SpyTtsAdapter()

    # One turn = 2 speech frames + 2 silence frames (silence_onset_ms=64, frame_duration_ms=32).
    one_turn = [0.9, 0.9, 0.1, 0.1]
    vad_probs = one_turn * 3

    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        streaming_model=_FakeStreamingModelDelayed(delay_s=0.01),
        tts_adapter=spy_tts,
        proposal_batch_window_ms=200,
    )

    await orch.start()

    frame_idx = 0
    for _ in range(3):
        for _ in one_turn:
            evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(frame_idx))
            await audio_in.put((_pcm_chunk(), evt.event_id))
            frame_idx += 1
        await asyncio.sleep(0.4)  # let TTS complete + _decision_in_flight reset

    await orch.stop()

    synth_count = spy_tts.synthesize_call_count
    skipped = [e for e in received if e.event_type == "synthesis_skipped_no_proposal"]
    assert skipped == [], (
        f"synthesis_skipped_no_proposal fired — turn-N+1 regression: {[e.payload_inline for e in skipped]}"
    )
    assert synth_count >= 2, (
        f"Expected TTS on at least 2 turns, got {synth_count}. Likely turn-N+1 silent regression."
    )


# ---------------------------------------------------------------------------
# Test: _best_proposal_text unit tests (2026-05-17 truncation regression)
# ---------------------------------------------------------------------------


def test_best_proposal_text_concatenates_all_chunks():
    """Regression: previous max(confidence) impl always returned proposals[0]
    because confidence is hardcoded to 0.9, silently dropping every chunk
    after the first. See 2026-05-17 truncation incident."""
    from companion_harness.realtime_orchestrator import _best_proposal_text

    proposals = [
        ThinkerProposal(
            proposal_type="observation", content="Yeah, I", trigger="eou",
            confidence=0.9, novelty=0.5, interruption_cost=0.1,
            max_utterance_ms=2000, cooldown_consumed="full_response", caused_by=[],
        ),
        ThinkerProposal(
            proposal_type="observation", content="think that sounds", trigger="eou",
            confidence=0.9, novelty=0.5, interruption_cost=0.1,
            max_utterance_ms=2000, cooldown_consumed="full_response", caused_by=[],
        ),
        ThinkerProposal(
            proposal_type="observation", content="great", trigger="eou",
            confidence=0.9, novelty=0.5, interruption_cost=0.1,
            max_utterance_ms=2000, cooldown_consumed="full_response", caused_by=[],
        ),
    ]
    result = _best_proposal_text(proposals)
    assert "Yeah, I" in result
    assert "think that sounds" in result
    assert "great" in result
    assert len(result) > len(proposals[0].content)


def test_best_proposal_text_empty_list():
    from companion_harness.realtime_orchestrator import _best_proposal_text

    assert _best_proposal_text([]) == ""


def test_best_proposal_text_filters_empty_content():
    """Empty proposals (e.g., leading silence chunks) should not produce extra spaces."""
    from companion_harness.realtime_orchestrator import _best_proposal_text

    proposals = [
        ThinkerProposal(
            proposal_type="observation", content="", trigger="eou",
            confidence=0.9, novelty=0.5, interruption_cost=0.1,
            max_utterance_ms=2000, cooldown_consumed="full_response", caused_by=[],
        ),
        ThinkerProposal(
            proposal_type="observation", content="hello", trigger="eou",
            confidence=0.9, novelty=0.5, interruption_cost=0.1,
            max_utterance_ms=2000, cooldown_consumed="full_response", caused_by=[],
        ),
        ThinkerProposal(
            proposal_type="observation", content="", trigger="eou",
            confidence=0.9, novelty=0.5, interruption_cost=0.1,
            max_utterance_ms=2000, cooldown_consumed="full_response", caused_by=[],
        ),
    ]
    result = _best_proposal_text(proposals)
    assert result.strip() == "hello"


# ---------------------------------------------------------------------------
# Seam-gate regression tests (2026-05-17: dashboard toggle was a placebo)
# ---------------------------------------------------------------------------


class _SpyDetector:
    """Wraps a detector and records whether process_frame was called."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.call_count = 0

    def process_frame(self, frame_bytes: bytes, caused_by: list[str]):
        self.call_count += 1
        return self._inner.process_frame(frame_bytes, caused_by)

    # Proxy attribute access for last_frame_* used in barge-in path.
    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _build_orch_with_spies(
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    config_store: ConfigStore | None,
) -> tuple[StreamingRealtimeOrchestrator, "_SpyDetector", "_SpyDetector", "_SpyDetector"]:
    vad_inner = VADDetector(
        model=_FakeVADModel([0.9, 0.9, 0.1, 0.1]),
        session_id=session_id,
        logger=logger,
        speech_threshold=0.5,
        silence_onset_ms=64,
        frame_duration_ms=32,
    )
    smart_inner = SmartTurnDetector(
        model=_FakeSmartTurnModel(),
        session_id=session_id,
        logger=logger,
    )
    bc_inner = BackchannelClassifier(
        model=_FakeBackchannelModel(),
        session_id=session_id,
        logger=logger,
    )
    spy_vad = _SpyDetector(vad_inner)
    spy_smart = _SpyDetector(smart_inner)
    spy_bc = _SpyDetector(bc_inner)
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
        vad_detector=spy_vad,
        smart_turn_detector=spy_smart,
        backchannel_classifier=spy_bc,
        policy_inputs_builder=_build_policy_inputs,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        config_store=config_store,
    )
    return orch, spy_vad, spy_smart, spy_bc


@pytest.mark.asyncio
async def test_detector_seam_disabled_skips_invocation(tmp_path: Path):
    """Regression: seam=OFF must prevent detector.process_frame() invocation entirely,
    not just suppress its output. See 2026-05-17 seam-placebo finding."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    store = ConfigStore(ALLOWLIST, seam_defaults={"smart_turn": False})
    orch, spy_vad, spy_smart, spy_bc = _build_orch_with_spies(
        "test-seam-disabled", logger, session, audio_in, store
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 2)
    await asyncio.sleep(0.1)
    await orch.stop()

    assert spy_smart.call_count == 0, (
        f"smart_turn detector called {spy_smart.call_count} times despite seam=OFF"
    )
    assert spy_vad.call_count >= 1, "vad detector should still run when only smart_turn is disabled"


@pytest.mark.asyncio
async def test_detector_seam_enabled_invokes(tmp_path: Path):
    """Sanity: seam=ON invokes the detector normally."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    store = ConfigStore(ALLOWLIST)  # all seams enabled by default
    orch, spy_vad, spy_smart, spy_bc = _build_orch_with_spies(
        "test-seam-enabled", logger, session, audio_in, store
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 2)
    await asyncio.sleep(0.1)
    await orch.stop()

    assert spy_smart.call_count >= 1, "smart_turn detector not called despite seam=ON"
    assert spy_vad.call_count >= 1, "vad detector not called despite seam=ON"
    assert spy_bc.call_count >= 1, "backchannel detector not called despite seam=ON"


@pytest.mark.asyncio
async def test_detector_seam_default_true_when_no_config_store(tmp_path: Path):
    """When config_store is None (test fixtures), all detectors should run."""
    logger, _ = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    orch, spy_vad, spy_smart, spy_bc = _build_orch_with_spies(
        "test-seam-no-store", logger, session, audio_in, config_store=None
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, 2)
    await asyncio.sleep(0.1)
    await orch.stop()

    assert spy_vad.call_count >= 1, "vad detector not called without config_store"
    assert spy_smart.call_count >= 1, "smart_turn detector not called without config_store"
    assert spy_bc.call_count >= 1, "backchannel detector not called without config_store"
