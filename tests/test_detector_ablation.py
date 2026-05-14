"""Stage 1 — detector ablation: same fixture replayed through VAD and SmartTurn paths.

Each path emits a TurnSignal; TurnSignal.detector must identify the source detector.
Success criterion (ROADMAP Task 13): pytest -k detector_ablation passes non-vacuously.

See fixture detector_ablation_001/case.json fixture_conventions for frame encoding rules.
"""

import struct

import pytest

from companion_harness.event_logger import EventLogger
from companion_harness.fixtures.loader import load_fixture
from companion_harness.schemas import Event, TurnSignal
from companion_harness.turn_detector_smart import SmartTurnDetector, SmartTurnModel
from companion_harness.turn_detector_vad import VADDetector, VADModel

# ---------------------------------------------------------------------------
# Frame synthesis (fixture fixture_conventions.frame_bytes_synthesis)
# ---------------------------------------------------------------------------

_N_SAMPLES = 320  # 640 bytes per frame, 32ms @ 20kHz — fixture convention
_SPEECH_AMPLITUDE = 500  # RMS=500 >= VAD silence_rms_threshold; also >= SmartTurn threshold
_SILENCE_AMPLITUDE = 0


def _frame(vad_speech: bool) -> bytes:
    amplitude = _SPEECH_AMPLITUDE if vad_speech else _SILENCE_AMPLITUDE
    return struct.pack(f"<{_N_SAMPLES}h", *([amplitude] * _N_SAMPLES))


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

class _ScriptedVADModel:
    """Returns p_speech=0.9 for speech frames and 0.1 for silence frames."""

    def __call__(self, frame: bytes) -> float:
        # Detect by RMS: silence bytes are all zero.
        first_sample = struct.unpack_from("<h", frame, 0)[0]
        return 0.9 if first_sample != 0 else 0.1


assert isinstance(_ScriptedVADModel(), VADModel)


class _ScriptedSmartTurnModel:
    """Returns scripted (p_done, p_continue) pairs in order."""

    def __init__(self, pairs: list[tuple[float, float]]) -> None:
        self._pairs = list(pairs)
        self._idx = 0
        self.call_count = 0

    def __call__(self, audio_buffer: bytes) -> tuple[float, float]:
        pair = self._pairs[self._idx % len(self._pairs)]
        self._idx += 1
        self.call_count += 1
        return pair


assert isinstance(_ScriptedSmartTurnModel([]), SmartTurnModel)


# ---------------------------------------------------------------------------
# Logger helper
# ---------------------------------------------------------------------------

def _make_logger() -> tuple[EventLogger, list[Event]]:
    received: list[Event] = []

    async def sink(event: Event) -> None:
        received.append(event)

    return EventLogger(sink, maxsize=4096), received


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_detector_ablation():
    """detector_ablation_001: same trace replayed through VAD and SmartTurn paths.

    Non-vacuity guarantees:
      (a) VAD path emits exactly one TurnSignal with detector='vad'.
          Fails if VAD never fires or mislabels the signal.
      (b) SmartTurn path emits exactly one TurnSignal with detector='smart_turn'.
          Fails if SmartTurn never fires or mislabels the signal.
      (c) Both signals are emitted at or after the silence-candidate frame (t_ms=1088).
          Fails if a detector fires prematurely.
      (d) VAD TurnSignal has p_done > p_continue (end-of-turn, not pause).
          Fails if the stub values are wrong or the signal content is swapped.
      (e) SmartTurn TurnSignal carries p_done=0.85 > p_continue=0.10.
          Fails if the stub's scripted values are ignored or model not invoked.
    """
    fixture = load_fixture("detector_ablation_001")
    assert fixture["case_id"] == "detector_ablation_001"
    signal_trace = fixture["signal_trace"]

    # Identify the silence-candidate frame (frame-034, t_ms=1088).
    candidate_frame = next(
        f for f in signal_trace if f["frame_id"] == "frame-034"
    )
    assert candidate_frame["t_ms"] == 1088
    candidate_p_done = candidate_frame["p_done"]      # 0.85
    candidate_p_continue = candidate_frame["p_continue"]  # 0.10

    # ------------------------------------------------------------------
    # VAD path
    # ------------------------------------------------------------------
    logger_vad, _ = _make_logger()
    vad_detector = VADDetector(
        model=_ScriptedVADModel(),
        session_id="ablation-vad",
        logger=logger_vad,
        frame_duration_ms=32,
        silence_onset_ms=300,
    )

    vad_signals: list[tuple[int, TurnSignal]] = []  # (t_ms, signal)
    for frame in signal_trace:
        if frame["frame_id"] == "frame-035":
            # Post-candidate trailing marker; no assertion expected.
            continue
        result = vad_detector.process_frame(_frame(frame["vad_speech"]), [frame["frame_id"]])
        if result is not None:
            vad_signals.append((frame["t_ms"], result))

    # (a) Exactly one VAD TurnSignal.
    assert len(vad_signals) == 1, (
        f"VAD path: expected 1 TurnSignal, got {len(vad_signals)}"
    )
    vad_t, vad_sig = vad_signals[0]

    # Correct detector label.
    assert vad_sig.detector == "vad", (
        f"VAD TurnSignal.detector={vad_sig.detector!r}, expected 'vad'"
    )

    # (c) Fired at or after candidate frame.
    assert vad_t >= 1088, (
        f"VAD signal fired at t_ms={vad_t}, before silence-candidate t_ms=1088"
    )

    # (d) End-of-turn: p_done > p_continue.
    assert vad_sig.p_done > vad_sig.p_continue, (
        f"VAD TurnSignal: p_done={vad_sig.p_done} not > p_continue={vad_sig.p_continue}"
    )

    # ------------------------------------------------------------------
    # SmartTurn path
    # ------------------------------------------------------------------
    smart_model = _ScriptedSmartTurnModel([(candidate_p_done, candidate_p_continue)])
    logger_smart, _ = _make_logger()
    smart_detector = SmartTurnDetector(
        model=smart_model,
        session_id="ablation-smart",
        logger=logger_smart,
        silence_onset_ms=300,
        frame_duration_ms=32,
    )

    smart_signals: list[tuple[int, TurnSignal]] = []
    for frame in signal_trace:
        if frame["frame_id"] == "frame-035":
            continue
        result = smart_detector.process_frame(_frame(frame["vad_speech"]), [frame["frame_id"]])
        if result is not None:
            smart_signals.append((frame["t_ms"], result))

    # (b) Exactly one SmartTurn TurnSignal.
    assert len(smart_signals) == 1, (
        f"SmartTurn path: expected 1 TurnSignal, got {len(smart_signals)}"
    )
    smart_t, smart_sig = smart_signals[0]

    # Correct detector label.
    assert smart_sig.detector == "smart_turn", (
        f"SmartTurn TurnSignal.detector={smart_sig.detector!r}, expected 'smart_turn'"
    )

    # (c) Fired at or after candidate frame.
    assert smart_t >= 1088, (
        f"SmartTurn signal fired at t_ms={smart_t}, before silence-candidate t_ms=1088"
    )

    # (e) Scripted p_done=0.85 > p_continue=0.10.
    assert smart_sig.p_done == candidate_p_done, (
        f"SmartTurn p_done={smart_sig.p_done}, expected {candidate_p_done}"
    )
    assert smart_sig.p_continue == candidate_p_continue, (
        f"SmartTurn p_continue={smart_sig.p_continue}, expected {candidate_p_continue}"
    )
    assert smart_sig.p_done > smart_sig.p_continue

    # Model invoked exactly once (silence-candidate-only rule).
    assert smart_model.call_count == 1, (
        f"SmartTurn model invoked {smart_model.call_count} times, expected 1"
    )
