"""Stage 1 — assistant does NOT stop on a backchannel ("mm-hmm") but DOES respond
to a genuine semantic interruption.

See docs/roadmap-v0.1b-draft.md §v0.1b Task 11 and fixture backchannel_001.
Success criterion:
  - backchannel_false_stop_rate = 0 on backchannel_001 main sub-case.
  - Contrast sub-case produces action_type != 'backchannel' AND
    primary_reason_code != BACKCHANNEL_DETECTED.
"""

from companion_harness import speak_policy
from companion_harness.backchannel_classifier import BackchannelClassifier, BackchannelModel
from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import Event, PolicyInputs


# ---------------------------------------------------------------------------
# Stub BackchannelModel — returns the fixture's scripted p_backchannel values.
# ---------------------------------------------------------------------------

class _ScriptedBackchannelModel:
    """Returns scripted p_backchannel values in order; cycles if exhausted."""

    def __init__(self, probs: list[float]) -> None:
        self._probs = list(probs)
        self._idx = 0

    def __call__(self, frame: bytes) -> float:
        prob = self._probs[self._idx % len(self._probs)]
        self._idx += 1
        return prob


assert isinstance(_ScriptedBackchannelModel([]), BackchannelModel)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _inputs_from_frame(frame: dict) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=frame.get("user_speaking", False),
        eou_probability=frame.get("eou_probability", 0.0),
        assistant_speaking=frame.get("assistant_speaking", False),
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=frame.get("user_addressed_agent", False),
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode=frame.get("social_mode", "normal"),
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


_GATE_MARKER_EVENT_TYPES = frozenset({"assert_backchannel_no_stop"})

_ROLE_LABEL_EVENT_TYPES = frozenset({
    "user_backchannel_active",
    "user_interruption_start",
    "user_interruption_active",
    "user_interruption_end",
})


def _is_real_frame(frame: dict) -> bool:
    """True for frames that should be fed to the classifier and policy."""
    return frame.get("event_type") not in _GATE_MARKER_EVENT_TYPES


def _replay_sub_case(
    frames: list[dict],
    session_id: str,
) -> list[dict]:
    """Feed sub-case frames through BackchannelClassifier → SpeakPolicy.decide().

    Returns a list of decision dicts: {frame_id, t_ms, action_type, reason_code, p_backchannel}.
    Gate-marker frames are skipped (fixture_conventions discipline).
    """
    real_frames = [f for f in frames if _is_real_frame(f)]
    probs = [f["p_backchannel"] for f in real_frames]

    logger, _ = _make_logger()
    classifier = BackchannelClassifier(
        model=_ScriptedBackchannelModel(probs),
        session_id=session_id,
        logger=logger,
    )

    decisions = []
    for frame in real_frames:
        signal = classifier.process_frame(b"\x00" * 64, caused_by=[frame["frame_id"]])
        assert signal is not None
        decision = speak_policy.decide(
            _inputs_from_frame(frame),
            signal_event_ids=signal.evidence_event_ids,
            p_backchannel=signal.p_backchannel,
        )
        decisions.append({
            "frame_id": frame["frame_id"],
            "t_ms": frame["t_ms"],
            "action_type": decision.action_type,
            "reason_code": decision.primary_reason_code,
            "p_backchannel": signal.p_backchannel,
        })

    return decisions


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_backchannel_survival():
    """backchannel_001: assistant survives backchannel; responds to genuine interruption.

    Non-vacuity guarantees:
      (main)     policy returns action_type='backchannel' + BACKCHANNEL_DETECTED
                 on the high-p_backchannel frames — fails if the backchannel gate
                 is absent or the threshold is wrong.
      (contrast) policy returns action_type != 'backchannel' AND
                 primary_reason_code != BACKCHANNEL_DETECTED on low-p_backchannel
                 frames — fails if the gate fires unconditionally or the threshold
                 is too low.  Both assertions can independently fail.
    """
    fixture = load_fixture("backchannel_001")
    assert fixture["case_id"] == "backchannel_001"
    assert fixture["expected_metrics"]["backchannel_false_stop_rate"] == "= 0"

    signal_trace = fixture["signal_trace"]

    main_frames = [f for f in signal_trace if f.get("sub_case") == "main"]
    contrast_frames = [f for f in signal_trace if f.get("sub_case") == "contrast"]

    assert len(main_frames) >= 1
    assert len(contrast_frames) >= 1

    # ── MAIN sub-case: "mm-hmm" backchannel ──────────────────────────────────
    main_decisions = _replay_sub_case(main_frames, session_id="test-bc-main")

    # Gate: no "false stop" — every decision on a high-p_backchannel frame must
    # be action_type='backchannel'.  A false stop is any non-backchannel decision
    # produced when p_backchannel >= threshold (the classifier gate should fire).
    false_stops = [
        d for d in main_decisions
        if d["p_backchannel"] >= 0.7 and d["action_type"] != "backchannel"
    ]
    assert len(false_stops) == 0, (
        f"backchannel_false_stop_rate > 0: non-backchannel decisions on high-p_backchannel "
        f"frames: {[(d['frame_id'], d['action_type']) for d in false_stops]}"
    )

    # Positive: the representative frame (frame-002, p_backchannel=0.85) must
    # produce the exact baseline decision.
    high_bc_decisions = [d for d in main_decisions if d["p_backchannel"] >= 0.7]
    assert len(high_bc_decisions) >= 1, "no high-p_backchannel frames reached policy"
    for d in high_bc_decisions:
        assert d["action_type"] == "backchannel", (
            f"{d['frame_id']}: expected action_type='backchannel', got {d['action_type']!r}"
        )
        assert d["reason_code"] == ReasonCode.BACKCHANNEL_DETECTED, (
            f"{d['frame_id']}: expected BACKCHANNEL_DETECTED, got {d['reason_code']!r}"
        )

    # ── CONTRAST sub-case: genuine semantic interruption ─────────────────────
    contrast_decisions = _replay_sub_case(contrast_frames, session_id="test-bc-contrast")

    # The contrast case discriminator: low-p_backchannel + high-eou frames must
    # NOT produce action_type='backchannel'.  This makes the test non-vacuous.
    low_bc_decisions = [d for d in contrast_decisions if d["p_backchannel"] < 0.7]
    assert len(low_bc_decisions) >= 1, "no low-p_backchannel frames in contrast sub-case"

    for d in low_bc_decisions:
        assert d["action_type"] != "backchannel", (
            f"contrast {d['frame_id']}: backchannel gate fired on p_backchannel="
            f"{d['p_backchannel']:.2f} — gate threshold is wrong or gate is unconditional"
        )
        assert d["reason_code"] != ReasonCode.BACKCHANNEL_DETECTED, (
            f"contrast {d['frame_id']}: BACKCHANNEL_DETECTED on a genuine interruption frame"
        )

    # Concrete positive: the representative contrast frame (frame-102, eou=0.88,
    # user_addressed_agent=True, p_backchannel=0.10) must produce full_response.
    rep_contrast = next(
        (d for d in contrast_decisions if d["frame_id"] == "frame-102"), None
    )
    assert rep_contrast is not None, "representative contrast frame-102 not found in decisions"
    assert rep_contrast["action_type"] == "full_response", (
        f"frame-102: expected full_response, got {rep_contrast['action_type']!r}"
    )
    assert rep_contrast["reason_code"] == ReasonCode.EOU_CONFIRMED, (
        f"frame-102: expected EOU_CONFIRMED, got {rep_contrast['reason_code']!r}"
    )
