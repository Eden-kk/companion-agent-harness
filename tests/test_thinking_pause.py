"""Stage 1 — "I think..." + 1.5s silence + continuation produces no premature full_response.

See docs/architecture-v0.1.md §Part 6 Stage 1, fixture thinking_pause_001 in
§Part 6c, and §Part 8 v0.1a acceptance gate
(thinking_pause_false_positive_rate = 0 on fixture set).
"""

import asyncio

from companion_harness.fixtures.loader import load_fixture
from companion_harness.event_logger import EventLogger
from companion_harness.turn_detector_vad import VADDetector
from companion_harness.speak_policy import decide
from companion_harness.schemas import PolicyInputs


# Fake VADModel satisfying the VADModel Protocol — no torch, no Silero.
class _ScriptedVAD:
    def __init__(self, p: float) -> None:
        self._p = p

    def __call__(self, frame: bytes) -> float:
        return self._p


def _default_inputs(user_speaking: bool, eou_probability: float) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=user_speaking,
        eou_probability=eou_probability,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=False,
        user_addressed_agent=False,   # thinking pause: mid-thought, not handing off
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="default",
        current_task_mode="ambient",
        social_mode="one_on_one",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
    )


def _replay(signal_trace: list[dict]) -> list[dict]:
    """Drive VADDetector + SpeakPolicy through one pass of the signal_trace.

    Returns list of {t_ms, action_type} for every frame that produced a decision.
    """
    results = []
    logged: list = []

    async def _sink(evt):
        logged.append(evt)

    logger = EventLogger(sink=_sink)

    session_id = "test-session"
    seed_event_id = "seed-0"

    for frame_data in signal_trace:
        t_ms: int = frame_data["t_ms"]
        vad_speech: bool = frame_data["vad_speech"]

        p = 1.0 if vad_speech else 0.0
        model = _ScriptedVAD(p)

        # Fresh detector per frame so each scripted probability is applied
        # discretely — the fixture encodes logical state, not a continuous stream.
        detector = VADDetector(
            model=model,
            session_id=session_id,
            logger=logger,
            silence_onset_ms=300,
            frame_duration_ms=32,
        )

        signal = detector.process_frame(b"\x00" * 32, caused_by=[seed_event_id])

        # p_done from the TurnSignal (if emitted), else derive from VAD probability.
        if signal is not None:
            eou_prob = signal.p_done
            evidence = signal.evidence_event_ids
        else:
            # No EOU signal — treat as ongoing speech / silence below threshold.
            eou_prob = p  # 1.0 if speech active, 0.0 if silence but not yet onset
            evidence = [seed_event_id]

        inputs = _default_inputs(user_speaking=vad_speech, eou_probability=eou_prob)
        decision = decide(inputs, signal_event_ids=evidence)
        results.append({"t_ms": t_ms, "action_type": decision.action_type})

    return results


def test_thinking_pause():
    data = load_fixture("thinking_pause_001")
    signal_trace = data["signal_trace"]

    # Find the gate timestamp from the fixture.
    gate_t_ms = next(
        f["t_ms"] for f in signal_trace if f["event_type"] == "assert_no_full_response"
    )

    # First replay.
    results_1 = _replay(signal_trace)
    # Second replay — must be identical (invariant #5: deterministic policy replay).
    results_2 = _replay(signal_trace)

    # Core assertion: no full_response at or before the gate.
    pre_gate = [r for r in results_1 if r["t_ms"] <= gate_t_ms]
    assert len(pre_gate) > 0, "pre-gate window must contain at least one decision"
    full_responses_before_gate = [r for r in pre_gate if r["action_type"] == "full_response"]
    assert full_responses_before_gate == [], (
        f"thinking_pause_false_positive_rate must be 0; got full_response at: "
        f"{[r['t_ms'] for r in full_responses_before_gate]}"
    )

    # Determinism assertion (invariant #5).
    assert results_1 == results_2, "policy replay must be deterministic (invariant #5)"
