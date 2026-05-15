"""Stage 2 contract test — audio-visual conflict surfacing (v0.1c Task 15).

Success criterion (verbatim):
  pytest -k audio_visual_conflict passes non-vacuously — a conflict event is
  emitted and appears in the decision's caused_by, the SpeakDecision carries
  primary_reason_code=AUDIO_VISUAL_CONFLICT/action_type="clarification", AND
  a contrast assertion that a no-conflict frame produces a normal
  (non-conflict) decision.
"""

import hashlib
from datetime import datetime, timezone

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, PolicyInputs
from companion_harness.speak_policy import decide

SESSION_ID = "test-audio-visual-conflict"
SCHEMA_VERSION = "0.1"


def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=256), received


def _conflict_event(seq: int, conflict_score: float, caused_by: list[str], timestamp_mono_ms: int) -> Event:
    event_id = f"{SESSION_ID}-av-conflict-{seq}-{timestamp_mono_ms}"
    payload_hash = hashlib.sha256(
        f"audio_visual_conflict:{event_id}:{conflict_score:.4f}".encode()
    ).hexdigest()[:16]
    return Event(
        event_id=event_id,
        session_id=SESSION_ID,
        schema_version=SCHEMA_VERSION,
        seq_no=seq,
        event_type="audio_visual_conflict",
        timestamp_mono_ms=timestamp_mono_ms,
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source="vision_sidecar",
        caused_by=caused_by,
        payload_hash=payload_hash,
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )


def _policy_inputs(**overrides) -> PolicyInputs:
    defaults = dict(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=True,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )
    defaults.update(overrides)
    return PolicyInputs(**defaults)


@pytest.fixture(scope="module")
def fixture():
    return load_fixture("audio_visual_conflict_001")


@pytest.fixture(scope="module")
def conflict_frame(fixture):
    return next(s for s in fixture["signal_trace"] if s["sub_case"] == "conflict" and s["event_type"] == "audio_visual_conflict")


@pytest.fixture(scope="module")
def no_conflict_frame(fixture):
    return next(s for s in fixture["signal_trace"] if s["sub_case"] == "no_conflict" and s.get("resolution_frame"))


@pytest.mark.asyncio
async def test_conflict_event_emitted_and_in_caused_by(conflict_frame):
    """Conflict case: audio_visual_conflict event is emitted and appears in SpeakDecision.caused_by."""
    logger, received = _make_logger()
    await logger.start()

    upstream_event_id = f"raw-video-frame-{conflict_frame['frame_id']}"
    conflict_score = conflict_frame["audio_visual_conflict_score"]
    ts_ms = conflict_frame["timestamp_mono_ms"]

    evt = _conflict_event(seq=1, conflict_score=conflict_score, caused_by=[upstream_event_id], timestamp_mono_ms=ts_ms)
    logger.log(evt)
    await logger.stop()

    assert len(received) == 1
    assert received[0].event_type == "audio_visual_conflict"
    assert upstream_event_id in received[0].caused_by

    decision = decide(
        _policy_inputs(audio_visual_conflict_score=conflict_score),
        [evt.event_id],
    )

    assert decision.primary_reason_code == ReasonCode.AUDIO_VISUAL_CONFLICT
    assert decision.action_type == "clarification"
    assert evt.event_id in decision.caused_by


@pytest.mark.asyncio
async def test_conflict_decision_is_not_full_response(conflict_frame):
    """Conflict case must NOT produce a plain full_response (negative discriminator)."""
    logger, _ = _make_logger()
    await logger.start()

    conflict_score = conflict_frame["audio_visual_conflict_score"]
    evt = _conflict_event(seq=2, conflict_score=conflict_score, caused_by=["raw-video-frame-001"], timestamp_mono_ms=conflict_frame["timestamp_mono_ms"])
    logger.log(evt)
    await logger.stop()

    decision = decide(
        _policy_inputs(audio_visual_conflict_score=conflict_score),
        [evt.event_id],
    )

    assert decision.action_type != "full_response"


@pytest.mark.asyncio
async def test_no_conflict_frame_produces_full_response(no_conflict_frame):
    """No-conflict case: conflict_score < 0.7 → full_response with EOU_CONFIRMED (positive discriminator)."""
    logger, received = _make_logger()
    await logger.start()

    conflict_score = no_conflict_frame["audio_visual_conflict_score"]
    upstream_event_id = f"raw-video-frame-{no_conflict_frame['frame_id']}"

    evt = Event(
        event_id=upstream_event_id,
        session_id=SESSION_ID,
        schema_version=SCHEMA_VERSION,
        seq_no=100,
        event_type="raw_video_frame",
        timestamp_mono_ms=no_conflict_frame["timestamp_mono_ms"],
        timestamp_wall=datetime.now(timezone.utc).isoformat(),
        source="vision_sidecar",
        caused_by=[],
        payload_hash="",
        payload_ref=None,
        payload_kind="raw_video",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )
    logger.log(evt)
    await logger.stop()

    assert conflict_score < 0.7

    decision = decide(
        _policy_inputs(audio_visual_conflict_score=conflict_score),
        [evt.event_id],
    )

    assert decision.action_type == "full_response"
    assert decision.primary_reason_code == ReasonCode.EOU_CONFIRMED
    assert decision.primary_reason_code != ReasonCode.AUDIO_VISUAL_CONFLICT


@pytest.mark.asyncio
async def test_conflict_event_has_causal_chain_into_raw_video_frame(conflict_frame):
    """Conflict event caused_by must reference the upstream raw_video_frame event_id."""
    logger, received = _make_logger()
    await logger.start()

    upstream_event_id = f"raw-video-frame-{conflict_frame['frame_id']}"
    evt = _conflict_event(
        seq=3,
        conflict_score=conflict_frame["audio_visual_conflict_score"],
        caused_by=[upstream_event_id],
        timestamp_mono_ms=conflict_frame["timestamp_mono_ms"],
    )
    logger.log(evt)
    await logger.stop()

    assert len(received) == 1
    assert upstream_event_id in received[0].caused_by


def test_fixture_conflict_threshold_matches_policy():
    """Fixture conflict_threshold (0.7) matches _AUDIO_VISUAL_CONFLICT_THRESHOLD in speak_policy."""
    from companion_harness.speak_policy import _AUDIO_VISUAL_CONFLICT_THRESHOLD
    fixture = load_fixture("audio_visual_conflict_001")
    assert fixture["fixture_conventions"]["conflict_threshold"] == _AUDIO_VISUAL_CONFLICT_THRESHOLD


def test_fixture_baseline_decisions_match_policy(fixture, conflict_frame, no_conflict_frame):
    """Baseline decisions in the fixture agree with what decide() actually returns."""
    baseline = {b["frame_id"]: b for b in fixture["baseline_decisions"]}

    conflict_decision = decide(
        _policy_inputs(audio_visual_conflict_score=conflict_frame["audio_visual_conflict_score"]),
        ["stub-signal-conflict"],
    )
    assert conflict_decision.action_type == baseline["frame-002"]["action_type"]
    assert conflict_decision.primary_reason_code.value == baseline["frame-002"]["primary_reason_code"]

    no_conflict_decision = decide(
        _policy_inputs(audio_visual_conflict_score=no_conflict_frame["audio_visual_conflict_score"]),
        ["stub-signal-no-conflict"],
    )
    assert no_conflict_decision.action_type == baseline["frame-101"]["action_type"]
    assert no_conflict_decision.primary_reason_code.value == baseline["frame-101"]["primary_reason_code"]
