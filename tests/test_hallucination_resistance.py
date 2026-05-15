"""Stage 2 — hallucination resistance as a policy behavior (v0.1c Task 12).

Success criterion (verbatim):
  pytest -k hallucination_resistance passes non-vacuously — a low-confidence
  frame produces an uncertainty/non-committal outcome with
  primary_reason_code=VISUAL_LOW_CONFIDENCE, AND a negative-path assertion:
  a high-confidence frame DOES produce a confident grounding (so the test fails
  if resistance were wired as blanket refusal).

Fixture: hallucination_resistance_001
  low_confidence sub-case:  grounding_confidence=0.30 < threshold=0.5 →
      action_type="silence", primary_reason_code=VISUAL_LOW_CONFIDENCE
  high_confidence sub-case: grounding_confidence=0.92 >= threshold=0.5 →
      action_type="full_response", primary_reason_code=EOU_CONFIRMED
"""

from companion_harness.fixtures.loader import load_fixture
from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import PolicyInputs
from companion_harness.speak_policy import decide
from companion_harness.vision_sidecar import FrameRef, VisionSidecar


class _ScriptedGroundingModel:
    """Returns scripted (label, confidence) pairs per call."""

    def __init__(self, results: list[tuple[str, float]]) -> None:
        self._iter = iter(results)

    def __call__(self, frame: bytes, query: str) -> tuple[str, float]:
        return next(self._iter)


def _make_sidecar(grounding_results: list[tuple[str, float]]) -> VisionSidecar:
    return VisionSidecar(
        scene_scorer=lambda prev, curr: 0.0,
        grounding_model=_ScriptedGroundingModel(grounding_results),
    )


def _policy_inputs(grounding_confidence: float, deictic: bool) -> PolicyInputs:
    return PolicyInputs(
        user_speaking=False,
        eou_probability=0.9,
        assistant_speaking=False,
        scene_change_score=0.0,
        deictic_reference=deictic,
        user_addressed_agent=True,
        urgency_score=0.0,
        proactivity_budget_remaining={},
        privacy_mode="normal",
        current_task_mode="normal",
        social_mode="user_addressing_agent",
        risk_mode="normal",
        cooldown_state={},
        attachment_risk_level=0.0,
        grounding_confidence=grounding_confidence,
    )


def test_hallucination_resistance():
    """Replay hallucination_resistance_001: low-confidence → silence/VISUAL_LOW_CONFIDENCE;
    high-confidence → full_response/EOU_CONFIRMED (proves resistance is not blanket refusal).
    """
    fixture = load_fixture("hallucination_resistance_001")
    assert fixture["case_id"] == "hallucination_resistance_001"

    signal_trace = fixture["signal_trace"]
    baseline_decisions = fixture["baseline_decisions"]
    confidence_threshold = fixture["fixture_conventions"]["confidence_threshold"]

    # Partition fixture into sub-cases.
    low_conf_frames = [f for f in signal_trace if f["sub_case"] == "low_confidence" and f.get("resolution_frame")]
    high_conf_frames = [f for f in signal_trace if f["sub_case"] == "high_confidence" and f.get("resolution_frame")]

    assert low_conf_frames, "fixture must contain at least one low_confidence resolution frame"
    assert high_conf_frames, "fixture must contain at least one high_confidence resolution frame"

    # --- Low-confidence path ---
    # Build a VisionSidecar that returns the scripted low-confidence result.
    low_frame = low_conf_frames[0]
    low_sidecar = _make_sidecar([(low_frame["grounding_result"], low_frame["grounding_confidence"])])
    ref = FrameRef(
        event_id=low_frame["frame_id"],
        timestamp_mono_ms=low_frame["timestamp_mono_ms"],
        frame_bytes=bytes.fromhex(low_frame["frame_bytes_hex"]),
    )
    low_sidecar.ingest_frame(ref)
    gr_low = low_sidecar.resolve("what is this?", deictic_reference=True)

    assert gr_low.confidence < confidence_threshold, (
        f"low-confidence case: grounding_confidence={gr_low.confidence} must be < threshold={confidence_threshold}"
    )

    low_inputs = _policy_inputs(gr_low.confidence, deictic=True)
    low_decision = decide(low_inputs, [low_frame["frame_id"]])

    assert low_decision.action_type == "silence", (
        f"low-confidence frame must produce silence, got {low_decision.action_type!r}; "
        "visual_hallucination_rate gate fails if full_response is produced"
    )
    assert low_decision.primary_reason_code == ReasonCode.VISUAL_LOW_CONFIDENCE, (
        f"expected VISUAL_LOW_CONFIDENCE, got {low_decision.primary_reason_code!r}"
    )

    # Cross-check against fixture baseline.
    low_baseline = next(b for b in baseline_decisions if b["frame_id"] == low_frame["frame_id"])
    assert low_decision.action_type == low_baseline["action_type"]
    assert low_decision.primary_reason_code == ReasonCode(low_baseline["primary_reason_code"])

    # --- High-confidence path (negative-path assertion: resistance is NOT blanket refusal) ---
    high_frame = high_conf_frames[0]
    high_sidecar = _make_sidecar([(high_frame["grounding_result"], high_frame["grounding_confidence"])])
    ref_high = FrameRef(
        event_id=high_frame["frame_id"],
        timestamp_mono_ms=high_frame["timestamp_mono_ms"],
        frame_bytes=bytes.fromhex(high_frame["frame_bytes_hex"]),
    )
    high_sidecar.ingest_frame(ref_high)
    gr_high = high_sidecar.resolve("what is this?", deictic_reference=True)

    assert gr_high.confidence >= confidence_threshold, (
        f"high-confidence case: grounding_confidence={gr_high.confidence} must be >= threshold={confidence_threshold}"
    )

    high_inputs = _policy_inputs(gr_high.confidence, deictic=True)
    high_decision = decide(high_inputs, [high_frame["frame_id"]])

    assert high_decision.action_type == "full_response", (
        f"high-confidence frame must produce full_response, got {high_decision.action_type!r}; "
        "blanket refusal is a bug — resistance must be conditional on confidence"
    )
    assert high_decision.primary_reason_code == ReasonCode.EOU_CONFIRMED, (
        f"expected EOU_CONFIRMED, got {high_decision.primary_reason_code!r}"
    )

    # Cross-check against fixture baseline.
    high_baseline = next(b for b in baseline_decisions if b["frame_id"] == high_frame["frame_id"])
    assert high_decision.action_type == high_baseline["action_type"]
    assert high_decision.primary_reason_code == ReasonCode(high_baseline["primary_reason_code"])
