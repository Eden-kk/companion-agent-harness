"""Design §6 test 6 — T_MAX backstop fires and forces floor handover."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_SPEAK = ScriptedTick(is_listen=False, audio_waveform=np.ones(CHUNK_SAMPLES, dtype=np.float32))
_LISTEN = ScriptedTick(is_listen=True)


def test_tmax_backstop():
    # A speaks every tick; B listens throughout.
    # t_max=4: at t=3 turn_len[A]==4 → arm break on A (no collision → t_max_hits++).
    # At t=4: break fires → A forced to listen.
    fake_a = ScriptedDuplexSession(ticks=[_SPEAK] * 6, name="A")
    fake_b = ScriptedDuplexSession(ticks=[_LISTEN] * 6, name="B")

    trace = DebateOrchestrator(
        "test motion",
        sessions={"A": fake_a, "B": fake_b},
        listen_prob_scale={"A": 1.0, "B": 1.0},
        k_grace=1,
        n_deadlock=8,
        t_max=4,
        max_ticks=6,
        moderator_seed_audio=np.zeros(CHUNK_SAMPLES, np.float32),
        moderator_nudge_audio=np.full(CHUNK_SAMPLES, 0.7, np.float32),
        rng_seed=0,
    ).run()

    assert "A" in trace.ticks[3].break_armed_for_next_tick
    assert "A" in trace.ticks[4].break_fired_this_tick
    assert trace.metrics.t_max_hits >= 1
    assert trace.metrics.harness_forced_breaks >= 1
    assert trace.metrics.turn_count_per_speaker.get("A", 0) >= 1
