"""Design §6 test 5 — deadlock detection + moderator nudge injection."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_NUDGE = np.full(CHUNK_SAMPLES, 0.7, dtype=np.float32)
_LISTEN = ScriptedTick(is_listen=True)


def test_deadlock():
    fake_a = ScriptedDuplexSession(ticks=[_LISTEN] * 5, name="A")
    fake_b = ScriptedDuplexSession(ticks=[_LISTEN] * 5, name="B")

    trace = DebateOrchestrator(
        "test motion",
        sessions={"A": fake_a, "B": fake_b},
        listen_prob_scale={"A": 1.0, "B": 1.0},
        k_grace=1,
        n_deadlock=4,
        t_max=0,
        max_ticks=5,
        moderator_seed_audio=np.zeros(CHUNK_SAMPLES, np.float32),
        moderator_nudge_audio=_NUDGE.copy(),
        rng_seed=0,
    ).run()

    # silence_run hits n_deadlock=4 at t=3 (ticks 0,1,2,3 → run=4)
    assert trace.ticks[3].moderator_nudge_fired is True

    # t=4 prefill receives nudge audio (injected at end of t=3's plumb phase)
    assert np.array_equal(fake_a.prefill_history[4], _NUDGE)
    assert np.array_equal(fake_b.prefill_history[4], _NUDGE)

    assert trace.metrics.moderator_nudges_fired == 1
    assert trace.metrics.deadlocks_detected == 1
