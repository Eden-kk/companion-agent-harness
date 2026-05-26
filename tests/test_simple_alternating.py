"""Contract test for SimpleAlternatingOrchestrator — fake-driven, no GPU."""

from __future__ import annotations

import numpy as np

from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from companion_harness.debate.simple_alternating_orchestrator import SimpleAlternatingOrchestrator
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_WAV = np.ones(CHUNK_SAMPLES, dtype=np.float32)

# Only speaker ticks: generate is not called for the listener during the other's turn.
# A is speaker on turn 0 (2 ticks), listener on turn 1 (never generates).
_A_TICKS = [
    ScriptedTick(is_listen=False, text="Social media polarizes society.", audio_waveform=_WAV),
    ScriptedTick(is_listen=False, text="Teen mental health has cratered.", audio_waveform=_WAV, end_of_turn=True),
]

# B is listener on turn 0 (never generates), speaker on turn 1 (2 ticks).
_B_TICKS = [
    ScriptedTick(is_listen=False, text="Connection benefits outweigh the harms.", audio_waveform=_WAV),
    ScriptedTick(is_listen=False, text="Democratization of voice is net positive.", audio_waveform=_WAV, end_of_turn=True),
]

TURN_SIGNAL = "Your turn — make ONE clear point in 1-2 sentences, then stop."


def test_simple_alternating_two_turns():
    fake_a = ScriptedDuplexSession(_A_TICKS, name="A")
    fake_b = ScriptedDuplexSession(_B_TICKS, name="B")

    trace = SimpleAlternatingOrchestrator(
        motion="test motion",
        sessions={"A": fake_a, "B": fake_b},
        first_speaker="A",
        max_turn_ticks=4,
        total_turns=2,
        turn_signal_text=TURN_SIGNAL,
    ).run()

    assert len(trace.turns) == 2
    assert trace.turns[0].speaker == "A"
    assert trace.turns[1].speaker == "B"
    assert trace.turns[0].natural_eot is True
    assert trace.turns[1].natural_eot is True
    assert "Social media" in trace.turns[0].text or "Teen" in trace.turns[0].text
    assert "Connection" in trace.turns[1].text or "Democratization" in trace.turns[1].text

    # A received turn signal on its first prefill (turn 0, tick 0)
    assert fake_a.prefill_text_history[0] is not None
    assert fake_a.prefill_text_history[0][0].startswith("Your turn")

    # B is prefilled during A's 2 ticks (turn 0), then prefilled on its own turn.
    # B's prefill at index 2 is turn 1 tick 0 — where the turn signal is injected.
    assert fake_b.prefill_text_history[2] is not None
    assert fake_b.prefill_text_history[2][0].startswith("Your turn")
