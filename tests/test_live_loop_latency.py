"""Live-loop latency analyzer contract tests (v0.1d Task 7 main).

Four tests per plan §11:
  1. Event-pair existence contract — drives StreamingRealtimeOrchestrator,
     verifies real emission paths, not hand-constructed Events.
  2. Scripted event stream → expected percentiles.
  3. Missing-event handling (zero full_response, and unpaired full_response).
  4. Trial filtering (silence/backchannel excluded; stop with no in-flight gen skipped).

Prerequisite: PR #91 (7cae029) — typed policy_decision_action_<X> sub-events — on main.
All tests run without torch/GPU.
"""

from __future__ import annotations

import asyncio
import uuid
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
from companion_harness.live_loop_metrics import (
    MIN_SAMPLES_FOR_GATE,
    compute_metrics,
)
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
# Shared fake adapters (mirrors test_policy_decision_subevents.py)
# ---------------------------------------------------------------------------


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _pcm_chunk(n_bytes: int = 512) -> bytes:
    return b"\x00" * n_bytes


def _meta(i: int = 0) -> CaptureMetadata:
    return CaptureMetadata(
        client_id="test-latency",
        timestamp_mono_ms=1000 + i * 32,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
    )


class _FakeVADModel:
    def __init__(self, probs: list[float]) -> None:
        self._probs = iter(probs)

    def __call__(self, frame: bytes) -> float:
        return next(self._probs, 0.0)


class _FakeSmartTurnModel:
    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        return 0.1, 0.9


class _FakeBackchannelModel:
    def __call__(self, frame: bytes) -> float:
        return 0.0


class _FakeStreamingModel:
    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> ThinkerProposal | None:
        return None

    async def infer_stream(
        self,
        frame_iter: AsyncIterator[tuple[bytes, bytes | None]],
        caused_by: list[str],
    ) -> AsyncGenerator[ThinkerProposal, None]:
        async def _gen() -> AsyncGenerator[ThinkerProposal, None]:
            async for _ in frame_iter:
                pass
            yield ThinkerProposal(
                proposal_type="observation",
                content="scripted response",
                trigger="eou",
                confidence=0.9,
                novelty=0.5,
                interruption_cost=0.1,
                max_utterance_ms=2000,
                cooldown_consumed="full_response",
                caused_by=caused_by,
            )

        return _gen()


class _FixedActionPolicy:
    def __init__(self, action_type: str) -> None:
        self._action_type = action_type  # type: ignore[assignment]

    def __call__(
        self,
        inputs: PolicyInputs,
        signal_event_ids: list[str],
        p_backchannel: float = 0.0,
    ) -> SpeakDecision:
        return SpeakDecision(
            action_type=self._action_type,  # type: ignore[arg-type]
            primary_reason_code=ReasonCode.EOU_CONFIRMED,
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
        privacy_mode="default",
        current_task_mode="default",
        social_mode="default",
        risk_mode="default",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


async def _noop_sink(chunk: bytes) -> None:
    pass


def _build_orch(
    *,
    session_id: str,
    logger: EventLogger,
    ingest_session,
    audio_in: asyncio.Queue,
    vad_probs: list[float],
    speak_policy=None,
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
        ingest_session=ingest_session,
        audio_in=audio_in,
        vad_detector=vad,
        smart_turn_detector=smart_turn,
        backchannel_classifier=bc,
        policy_inputs_builder=_build_policy_inputs,
        speak_policy=speak_policy,
        foreground_model=fg,
        audio_output=controller,
        tts_adapter=SilentTtsAdapter(chunk_count=1),
        proposal_batch_window_ms=200,
    )


async def _push_frames(
    audio_in: asyncio.Queue,
    ingest: InputIngest,
    session,
    vad_probs: list[float],
) -> None:
    for i, _ in enumerate(vad_probs):
        evt = ingest.ingest_chunk(session, _pcm_chunk(), _meta(i))
        await audio_in.put((_pcm_chunk(), evt.event_id))


# ---------------------------------------------------------------------------
# Event construction helpers for scripted tests
# ---------------------------------------------------------------------------


def _evt(
    event_type: str,
    timestamp_mono_ms: int,
    caused_by: list[str] | None = None,
    event_id: str | None = None,
) -> Event:
    eid = event_id or str(uuid.uuid4())
    return Event(
        event_id=eid,
        session_id="test-session",
        schema_version="0.1",
        seq_no=0,
        event_type=event_type,
        timestamp_mono_ms=timestamp_mono_ms,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source="test",
        caused_by=caused_by or [],
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="unknown",
        sensitivity="safe",
        retention_policy_id="default",
    )


# ---------------------------------------------------------------------------
# Test 1: Event-pair existence contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_pair_existence_contract(tmp_path: Path):
    """Drives StreamingRealtimeOrchestrator to verify real emission of all §3 event types.

    Asserts:
    - policy_decision event emitted with timestamp_mono_ms: int
    - policy_decision_action_full_response co-emitted with caused_by containing policy_decision id
    - assistant_audio_buffer_flushed emitted (from AudioOutputController.play())
    - orchestrator_started emitted
    - harness_init emitted (from InputIngest.open_session())
    """
    logger, received = _make_logger()
    await logger.start()

    ingest = InputIngest(logger=logger, blob_dir=tmp_path)
    session_id = "test-existence-contract"
    session = ingest.open_session("test-client")
    audio_in: asyncio.Queue[tuple[bytes, str]] = asyncio.Queue(maxsize=64)

    vad_probs = [0.9, 0.9, 0.9, 0.9, 0.1, 0.1, 0.1]
    orch = _build_orch(
        session_id=session_id,
        logger=logger,
        ingest_session=session,
        audio_in=audio_in,
        vad_probs=vad_probs,
        speak_policy=_FixedActionPolicy("full_response"),
    )

    await orch.start()
    await _push_frames(audio_in, ingest, session, vad_probs)
    await asyncio.sleep(0.5)
    await orch.stop()

    event_types = {e.event_type for e in received}

    # harness_init from InputIngest
    assert "harness_init" in event_types, "harness_init not emitted"
    harness_init_evt = next(e for e in received if e.event_type == "harness_init")
    assert isinstance(harness_init_evt.timestamp_mono_ms, int)

    # orchestrator_started
    assert "orchestrator_started" in event_types, "orchestrator_started not emitted"

    # policy_decision with timestamp_mono_ms: int
    policy_evts = [e for e in received if e.event_type == "policy_decision"]
    assert policy_evts, "policy_decision not emitted"
    assert isinstance(policy_evts[0].timestamp_mono_ms, int)

    # policy_decision_action_full_response co-emitted with correct caused_by
    sub_evts = [e for e in received if e.event_type == "policy_decision_action_full_response"]
    assert sub_evts, "policy_decision_action_full_response not emitted"
    assert isinstance(sub_evts[0].timestamp_mono_ms, int)
    parent_id = policy_evts[0].event_id
    assert parent_id in sub_evts[0].caused_by, (
        f"policy_decision_action_full_response caused_by {sub_evts[0].caused_by!r} "
        f"does not contain policy_decision id {parent_id!r}"
    )

    # assistant_audio_buffer_flushed (from AudioOutputController)
    assert "assistant_audio_buffer_flushed" in event_types, "assistant_audio_buffer_flushed not emitted"

    # Verify realtime_orchestrator.py does NOT import live_loop_metrics
    import importlib.util
    spec = importlib.util.find_spec("companion_harness.realtime_orchestrator")
    assert spec is not None
    with open(spec.origin) as f:  # type: ignore[arg-type]
        src = f.read()
    assert "live_loop_metrics" not in src, (
        "realtime_orchestrator.py MUST NOT import live_loop_metrics (invariant #10)"
    )


# ---------------------------------------------------------------------------
# Test 2: Scripted event stream → expected percentiles
# ---------------------------------------------------------------------------


def test_scripted_event_stream_expected_percentiles():
    """Scripted list[Event] with known timestamps → exact p50/p95/status/sample_count.

    Covers direct_question_latency AND vad_detected_user_speech_to_stop_ms.
    """
    # Build a causal chain: policy_decision → policy_decision_action_full_response → flush
    # 4 trials for direct_question_latency (>= MIN_SAMPLES_FOR_GATE=3)
    # Latencies: 500, 700, 900, 1100 ms
    # p50 = sorted[ceil(0.5*4)-1] = sorted[1] = 700
    # p95 = sorted[ceil(0.95*4)-1] = sorted[3] = 1100
    events: list[Event] = [
        _evt("harness_init", 0),
    ]

    t = 100
    for latency in [500, 700, 900, 1100]:
        pd_id = str(uuid.uuid4())
        fr_id = str(uuid.uuid4())
        flush_id = str(uuid.uuid4())
        events.append(_evt("policy_decision", t, event_id=pd_id))
        events.append(_evt("policy_decision_action_full_response", t + 1, caused_by=[pd_id], event_id=fr_id))
        events.append(_evt("assistant_audio_buffer_flushed", t + 1 + latency, caused_by=[pd_id], event_id=flush_id))
        t += latency + 500

    # 3 VAD barge-in trials: latencies 50, 100, 150 ms
    # p95 = sorted[ceil(0.95*3)-1] = sorted[2] = 150
    t2 = t + 500
    for latency in [50, 100, 150]:
        gen_id = str(uuid.uuid4())
        onset_id = str(uuid.uuid4())
        stop_id = str(uuid.uuid4())
        events.append(_evt("assistant_generation_start", t2, event_id=gen_id))
        events.append(_evt("vad_user_speech_onset", t2 + 10, event_id=onset_id))
        events.append(_evt("assistant_audio_stop_completed", t2 + 10 + latency, event_id=stop_id))
        events.append(_evt("assistant_audio_buffer_flushed", t2 + 10 + latency + 10))  # clears gen state
        t2 += latency + 300

    results = compute_metrics(events)

    dql = results["direct_question_latency"]
    assert dql.sample_count == 4, f"expected 4 trials, got {dql.sample_count}"
    assert dql.p50_ms == 700, f"expected p50=700, got {dql.p50_ms}"
    assert dql.p95_ms == 1100, f"expected p95=1100, got {dql.p95_ms}"
    # p50=700<800 MET AND p95=1100<1500 MET → status should be MET
    assert dql.status == "MET", f"expected MET, got {dql.status}"

    vad = results["vad_detected_user_speech_to_stop_ms"]
    assert vad.sample_count == 3, f"expected 3 VAD trials, got {vad.sample_count}"
    assert vad.p95_ms == 150, f"expected p95=150, got {vad.p95_ms}"
    assert vad.status == "MET", f"expected MET (p95=150 < 200), got {vad.status}"


# ---------------------------------------------------------------------------
# Test 3: Missing-event handling
# ---------------------------------------------------------------------------


def test_missing_event_handling_zero_full_response():
    """Zero policy_decision_action_full_response → NOT_MEASURED with correct reason."""
    events = [
        _evt("harness_init", 0),
        _evt("orchestrator_started", 10),
        _evt("assistant_generation_start", 100),
        _evt("assistant_audio_buffer_flushed", 300),
    ]
    results = compute_metrics(events)
    dql = results["direct_question_latency"]
    assert dql.status == "NOT_MEASURED"
    assert dql.status_reason == "no_full_response_decisions_in_session"
    assert dql.sample_count == 0


def test_missing_event_handling_unpaired_full_response():
    """full_response sub-event with no causally-downstream flush → not a trial; advisory note."""
    pd_id = str(uuid.uuid4())
    fr_id = str(uuid.uuid4())
    events = [
        _evt("harness_init", 0),
        _evt("policy_decision", 100, event_id=pd_id),
        _evt("policy_decision_action_full_response", 101, caused_by=[pd_id], event_id=fr_id),
        # No assistant_audio_buffer_flushed causally downstream of pd_id
        # (This flush has no causal link to the policy_decision above)
        _evt("assistant_audio_buffer_flushed", 500, caused_by=[]),
    ]
    results = compute_metrics(events)
    dql = results["direct_question_latency"]
    # 1 full_response decision but 0 paired flushes → 0 trials → NOT_MEASURED
    assert dql.status == "NOT_MEASURED"
    assert dql.sample_count == 0
    # Advisory note about unpaired decisions should appear
    assert "unpaired_full_response_decisions" in dql.status_reason or \
           "sample_count_below_min" in dql.status_reason


# ---------------------------------------------------------------------------
# Test 4: Trial filtering
# ---------------------------------------------------------------------------


def test_trial_filtering_only_full_response_counted():
    """Silence and backchannel sub-events do not contribute to direct_question_latency.

    Only full_response-derived flushes count.
    """
    # silence decision — no flush expected, not a trial
    pd_silence_id = str(uuid.uuid4())
    events: list[Event] = [
        _evt("harness_init", 0),
        _evt("policy_decision", 100, event_id=pd_silence_id),
        _evt("policy_decision_action_silence", 101, caused_by=[pd_silence_id]),
    ]

    # backchannel decision — produces a flush, but must be excluded
    pd_bc_id = str(uuid.uuid4())
    bc_sub_id = str(uuid.uuid4())
    flush_bc_id = str(uuid.uuid4())
    events += [
        _evt("policy_decision", 200, event_id=pd_bc_id),
        _evt("policy_decision_action_backchannel", 201, caused_by=[pd_bc_id], event_id=bc_sub_id),
        _evt("assistant_audio_buffer_flushed", 300, caused_by=[pd_bc_id], event_id=flush_bc_id),
    ]

    # 3 full_response decisions → paired flushes (MIN_SAMPLES_FOR_GATE=3)
    # latencies: 400, 600, 800 ms
    t = 600
    for latency in [400, 600, 800]:
        pd_id = str(uuid.uuid4())
        events.append(_evt("policy_decision", t, event_id=pd_id))
        events.append(_evt("policy_decision_action_full_response", t + 1, caused_by=[pd_id]))
        events.append(_evt("assistant_audio_buffer_flushed", t + 1 + latency, caused_by=[pd_id]))
        t += latency + 400

    results = compute_metrics(events)
    dql = results["direct_question_latency"]

    # Only the 3 full_response-derived flushes count
    assert dql.sample_count == 3, f"expected 3 trials, got {dql.sample_count}"
    # p50 of [400, 600, 800]: sorted[ceil(0.5*3)-1] = sorted[1] = 600
    assert dql.p50_ms == 600, f"expected p50=600, got {dql.p50_ms}"
    # p95 of [400, 600, 800]: sorted[ceil(0.95*3)-1] = sorted[2] = 800
    assert dql.p95_ms == 800, f"expected p95=800, got {dql.p95_ms}"
    assert dql.status == "MET", f"expected MET, got {dql.status}"


def test_trial_filtering_stop_without_generation_skipped():
    """assistant_audio_stop_completed with no in-flight generation → not a barge-in trial."""
    events = [
        _evt("harness_init", 0),
        # No assistant_generation_start before this stop
        _evt("vad_user_speech_onset", 100),
        _evt("assistant_audio_stop_completed", 150),
    ]
    results = compute_metrics(events)
    vad = results["vad_detected_user_speech_to_stop_ms"]
    # No generation in flight when onset fired → 0 barge-in trials
    assert vad.sample_count == 0
    assert vad.status == "NOT_MEASURED"
    assert "sample_count_below_min" in vad.status_reason
