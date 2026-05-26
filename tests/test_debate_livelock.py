"""Design §6 test 4 — livelock / k_grace break resolution."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_SPEAK = ScriptedTick(is_listen=False, audio_waveform=np.ones(CHUNK_SAMPLES, dtype=np.float32))


def test_livelock():
    fake_a = ScriptedDuplexSession(ticks=[_SPEAK] * 5, name="A")
    fake_b = ScriptedDuplexSession(ticks=[_SPEAK] * 5, name="B")

    trace = DebateOrchestrator(
        "test motion",
        sessions={"A": fake_a, "B": fake_b},
        listen_prob_scale={"A": 1.0, "B": 1.0},
        k_grace=1,
        n_deadlock=4,
        t_max=0,
        max_ticks=3,
        moderator_seed_audio=np.zeros(CHUNK_SAMPLES, np.float32),
        moderator_nudge_audio=np.full(CHUNK_SAMPLES, 0.7, np.float32),
        rng_seed=0,
    ).run()

    incumbent = trace.ticks[0].floor

    assert trace.metrics.collision_count == 2
    assert trace.metrics.max_collision_duration_ticks <= trace.k_grace + 1
    assert trace.metrics.harness_forced_breaks == 1
    assert incumbent in trace.ticks[2].break_fired_this_tick
