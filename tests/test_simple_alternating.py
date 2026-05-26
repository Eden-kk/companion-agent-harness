"""Contract test for SimpleAlternatingOrchestrator — fake-driven, no GPU."""

from __future__ import annotations

import numpy as np

from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from companion_harness.debate.simple_alternating_orchestrator import SimpleAlternatingOrchestrator
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_WAV = np.ones(CHUNK_SAMPLES, dtype=np.float32)

# Each tick emits a sentence-ending chunk so the sentence-completion detector
# closes the turn after exactly one tick. The script intentionally provides
# more ticks than will be consumed; the orchestrator only calls generate()
# on the active speaker.
_A_TICKS = [
    ScriptedTick(is_listen=False, text="Social media polarizes society.", audio_waveform=_WAV),
    ScriptedTick(is_listen=False, text="Teen mental health has cratered.", audio_waveform=_WAV, end_of_turn=True),
]
_B_TICKS = [
    ScriptedTick(is_listen=False, text="Connection benefits outweigh the harms.", audio_waveform=_WAV),
    ScriptedTick(is_listen=False, text="Voice democratization is net positive.", audio_waveform=_WAV, end_of_turn=True),
]


def test_simple_alternating_two_turns_with_handoff():
    fake_a = ScriptedDuplexSession(_A_TICKS, name="A")
    fake_b = ScriptedDuplexSession(_B_TICKS, name="B")

    trace = SimpleAlternatingOrchestrator(
        motion="test motion",
        sessions={"A": fake_a, "B": fake_b},
        first_speaker="A",
        max_turn_ticks=4,
        total_turns=2,
    ).run()

    # Two complete turns, A then B.
    assert len(trace.turns) == 2
    assert trace.turns[0].speaker == "A"
    assert trace.turns[1].speaker == "B"

    # Each turn ended on a complete sentence (period at chunk end).
    assert trace.turns[0].ended_on_sentence is True or trace.turns[0].natural_eot is True
    assert trace.turns[1].ended_on_sentence is True or trace.turns[1].natural_eot is True
    assert "Social media polarizes society." in trace.turns[0].text
    assert "Connection benefits outweigh the harms." in trace.turns[1].text

    # First turn: A's very first prefill carries the first-turn signal.
    assert fake_a.prefill_text_history[0] is not None
    assert fake_a.prefill_text_history[0][0].lower().startswith("you are the proposition")

    # Handoff: B's very first prefill carries the handoff marker citing A by
    # name and quoting A's accumulated text.
    assert fake_b.prefill_text_history[0] is not None
    marker = fake_b.prefill_text_history[0][0]
    assert "(A)" in marker
    assert "Social media polarizes society." in marker

    # B's first prefill also carries the actual audio chunk A produced
    # (not silence) — confirms the audio replay is wired.
    assert not np.array_equal(fake_b.prefill_history[0], np.zeros(CHUNK_SAMPLES, dtype=np.float32))


def test_simple_alternating_listener_not_generated():
    """During A's turn, B.generate() must not be called at all (the design
    decision that prevents B's KV from drifting into 'listen mode')."""
    fake_a = ScriptedDuplexSession(_A_TICKS, name="A")
    fake_b = ScriptedDuplexSession(_B_TICKS, name="B")

    trace = SimpleAlternatingOrchestrator(
        motion="m",
        sessions={"A": fake_a, "B": fake_b},
        first_speaker="A",
        max_turn_ticks=2,
        total_turns=2,
    ).run()

    # A generated only on its own turn (turn 0).
    # B generated only on its own turn (turn 1).
    # The fake's _idx counter tracks generate() calls.
    a_generates = fake_a._idx
    b_generates = fake_b._idx
    assert a_generates == trace.turns[0].ticks_consumed
    assert b_generates == trace.turns[1].ticks_consumed
