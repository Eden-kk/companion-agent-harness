"""Stage 1 — "I think..." + 1.5s silence + continuation produces no premature full_response.

See docs/architecture-v0.1.md §Part 6 Stage 1, fixture thinking_pause_001 in
§Part 6c, and §Part 8 v0.1b acceptance gate
(thinking_pause_false_positive_rate = 0 on fixture set).

Wired through SmartTurnDetector (v0.1b Task 3) as required by issue #10.
"""

import struct

from companion_harness import speak_policy
from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.schemas import Event, PolicyInputs, TurnSignal
from companion_harness.turn_detector_smart import SmartTurnDetector, SmartTurnModel


# ---------------------------------------------------------------------------
# Stub SmartTurnModel — returns scripted (p_done, p_continue) per invocation.
# ---------------------------------------------------------------------------

class _ScriptedModel:
    """Stub SmartTurnModel: returns scripted pairs in invocation order."""

    def __init__(self, script: list[tuple[float, float]]) -> None:
        self._script = list(script)
        self._idx = 0

    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        pair = self._script[self._idx % len(self._script)]
        self._idx += 1
        return pair


assert isinstance(_ScriptedModel([]), SmartTurnModel)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


def _audio_frame(vad_speech: bool, frame_duration_ms: int = 32) -> bytes:
    """Return minimal PCM frame with RMS above (speech) or below (silence) the detector threshold.

    SmartTurnDetector._SILENCE_RMS_THRESHOLD = 500.  Speech: amplitude 1000; silence: 0.
    """
    n_samples = (frame_duration_ms * 16000) // 1000
    amplitude = 1000 if vad_speech else 0
    return struct.pack(f"<{n_samples}h", *([amplitude] * n_samples))


def _inputs(eou_probability: float) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
        eou_probability=eou_probability,
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


def _replay(
    trace: list[dict],
    model: SmartTurnModel,
    logger: EventLogger,
) -> tuple[list[TurnSignal], list[dict]]:
    """Feed trace through a persistent SmartTurnDetector; return emitted signals and decisions.

    Gate-marker frames (event_type='assert_no_full_response') are NOT fed to the
    detector — fixture_conventions discipline.
    """
    detector = SmartTurnDetector(
        model=model,
        session_id="test-thinking-pause",
        logger=logger,
    )
    signals: list[TurnSignal] = []
    decisions: list[dict] = []

    for frame in trace:
        if frame.get("event_type") == "assert_no_full_response":
            continue

        signal = detector.process_frame(_audio_frame(frame["vad_speech"]), [frame["frame_id"]])

        if signal is not None:
            signals.append(signal)
            decision = speak_policy.decide(
                _inputs(eou_probability=signal.p_done),
                signal_event_ids=signal.evidence_event_ids,
            )
            decisions.append({"t_ms": frame["t_ms"], "action_type": decision.action_type, "signal": signal})

    return signals, decisions


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_thinking_pause():
    """thinking_pause_001: no full_response during 1.5s thinking pause.

    Non-vacuity guarantees:
      (a) No full_response in pause window — fails if policy emits one on low-p_done signal.
      (b) Positive: a TurnSignal WAS emitted in pause window with p_continue > p_done —
          fails if detector never fired (compression / fresh-per-frame bug).
      (c) Negative-path: all-high-p_done frames DO produce full_response —
          fails if thinking-pause suppression is over-broad or broken.
    """
    fixture = load_fixture("thinking_pause_001")
    assert fixture["case_id"] == "thinking_pause_001"
    assert fixture["expected_metrics"]["thinking_pause_false_positive_rate"] == "= 0"

    signal_trace = fixture["signal_trace"]

    PAUSE_START_MS = 800
    PAUSE_END_MS = 2272
    GATE_MS = 2300

    # Collect pause-window probabilities from the fixture for the scripted model.
    pause_probs = [
        (f["p_done"], f["p_continue"])
        for f in signal_trace
        if f.get("event_type") != "assert_no_full_response"
        and PAUSE_START_MS <= f["t_ms"] <= PAUSE_END_MS
    ]
    assert len(pause_probs) >= 1

    logger, _ = _make_logger()
    _, decisions = _replay(signal_trace, _ScriptedModel(pause_probs), logger)

    # (a) No full_response at or before the gate marker.
    full_response_in_pause = [
        d for d in decisions
        if d["t_ms"] <= GATE_MS and d["action_type"] == "full_response"
    ]
    assert len(full_response_in_pause) == 0, (
        f"thinking_pause_false_positive_rate > 0: full_response at "
        f"t_ms={[d['t_ms'] for d in full_response_in_pause]}"
    )

    # (b) A TurnSignal WAS emitted in the pause window with p_continue > p_done.
    pause_signals = [
        d["signal"]
        for d in decisions
        if PAUSE_START_MS <= d["t_ms"] <= PAUSE_END_MS
    ]
    assert len(pause_signals) >= 1, (
        "no TurnSignal emitted during pause window — detector never fired "
        "(possible compression or fresh-per-frame bug)"
    )
    assert any(s.p_continue > s.p_done for s in pause_signals), (
        "no pause-window TurnSignal has p_continue > p_done — mechanism not genuinely exercised"
    )

    # (c) Negative-path: replace pause-window scripted probs with all-high-p_done;
    #     policy MUST emit full_response — proving the test fails when suppression is broken.
    high_p_done_trace = [
        dict(f, p_done=0.95, p_continue=0.02)
        if f.get("event_type") != "assert_no_full_response" and PAUSE_START_MS <= f["t_ms"] <= PAUSE_END_MS
        else f
        for f in signal_trace
    ]

    logger2, _ = _make_logger()
    _, decisions2 = _replay(
        high_p_done_trace,
        _ScriptedModel([(0.95, 0.02)] * len(pause_probs)),
        logger2,
    )

    full_response_negative = [d for d in decisions2 if d["action_type"] == "full_response"]
    assert len(full_response_negative) >= 1, (
        "negative-path: all-high-p_done frames did NOT produce full_response — "
        "thinking-pause suppression is over-broad or broken"
    )
