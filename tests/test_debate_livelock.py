"""Design §6 test 4 — livelock / k_grace=0 break resolution with KV rollback."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_SPEAK = ScriptedTick(is_listen=False, text="hello", audio_waveform=np.ones(CHUNK_SAMPLES, dtype=np.float32))


def test_livelock():
    fake_a = ScriptedDuplexSession(ticks=[_SPEAK] * 5, name="A")
    fake_b = ScriptedDuplexSession(ticks=[_SPEAK] * 5, name="B")

    trace = DebateOrchestrator(
        "test motion",
        sessions={"A": fake_a, "B": fake_b},
        listen_prob_scale={"A": 1.0, "B": 1.0},
        k_grace=0,
        n_deadlock=8,
        silence_terminate_ticks=5,
        t_max=0,
        max_ticks=4,
        moderator_seed_audio=np.zeros(CHUNK_SAMPLES, np.float32),
        moderator_nudge_audio=np.full(CHUNK_SAMPLES, 0.7, np.float32),
        rng_seed=0,
    ).run()

    # incumbent = the old floor-holder who was rolled back (NOT the new floor in ticks[0])
    assert len(trace.ticks[0].rolled_back_this_tick) == 1
    incumbent = trace.ticks[0].rolled_back_this_tick[0]

    # k_grace=0: collision resolves in 1 tick (overlap > 0 is immediately True)
    assert trace.metrics.max_collision_duration_ticks <= 1
    assert trace.metrics.harness_forced_breaks >= 1
    assert trace.metrics.rollbacks_performed >= 1
    assert trace.metrics.barge_text_signals_sent >= 1

    # t=1: break fires on incumbent (forced listen). The barge text is HELD
    # (incumbent's prefill is no-op'd by the break_event prefill guard).
    assert incumbent in trace.ticks[1].break_fired_this_tick
    incumbent_fake = fake_a if incumbent == "A" else fake_b
    assert incumbent_fake.prefill_text_history[1] is None

    # t=2: incumbent is free; barge text is delivered into its prefill.
    assert incumbent_fake.prefill_text_history[2] == [_SPEAK.text]
