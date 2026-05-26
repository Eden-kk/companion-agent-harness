"""Silence-termination gate — both silent for ≥5 ticks exits the loop early."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_LISTEN = ScriptedTick(is_listen=True)


def test_silence_terminate():
    fake_a = ScriptedDuplexSession(ticks=[_LISTEN] * 20, name="A")
    fake_b = ScriptedDuplexSession(ticks=[_LISTEN] * 20, name="B")

    trace = DebateOrchestrator(
        "test motion",
        sessions={"A": fake_a, "B": fake_b},
        listen_prob_scale={"A": 1.0, "B": 1.0},
        k_grace=0,
        silence_terminate_ticks=5,
        t_max=0,
        max_ticks=20,
        moderator_seed_audio=np.zeros(CHUNK_SAMPLES, np.float32),
        rng_seed=0,
    ).run()

    assert len(trace.ticks) == 5
    assert trace.metrics.silence_terminated is True
    assert trace.metrics.moderator_nudges_fired == 0
