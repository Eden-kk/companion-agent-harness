"""Design §6 test 3 — clean floor handover (self-yield after overlap)."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_SPEAK = ScriptedTick(is_listen=False, audio_waveform=np.ones(CHUNK_SAMPLES, dtype=np.float32))
_LISTEN = ScriptedTick(is_listen=True)


def test_clean_handover():
    # t=0: A speaks alone
    # t=1: A still speaks, B starts (collision — overlap just started)
    # t=2: A self-yields (goes listen without break fired); B speaks alone
    # k_grace=1: overlap[B]=1 at t=1, 1 > 1 is False → no break armed → A yields voluntarily
    fake_a = ScriptedDuplexSession(ticks=[_SPEAK, _SPEAK, _LISTEN], name="A")
    fake_b = ScriptedDuplexSession(ticks=[_LISTEN, _SPEAK, _SPEAK], name="B")

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

    t2 = trace.ticks[2]
    assert len(t2.break_fired_this_tick) == 0
    assert t2.audible == ["B"]
    assert trace.metrics.harness_forced_breaks == 0
    assert trace.metrics.self_yields >= 1
    assert trace.metrics.barge_in_attempts >= 1
