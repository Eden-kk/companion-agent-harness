"""Rollback + barge-signal injection — collision at t=1 rolls back A; the
break-fire tick (t=2) no-ops A's prefill, so the barge text is HELD and
delivered at t=3 prefill when A is free again."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_AUDIO = np.ones(CHUNK_SAMPLES, dtype=np.float32)


def test_rollback_and_barge_signal():
    # t=0: A speaks alone ("hello"), B listens
    # t=1: A speaks ("world"), B speaks ("STOP") → collision → k_grace=0 → arm+rollback A, floor=B
    # t=2: A force-listened (break fires); A's prefill is no-op'd so text is HELD
    # t=3: A free; B's barge text ("STOP") is delivered into A's prefill
    fake_a = ScriptedDuplexSession(
        ticks=[
            ScriptedTick(is_listen=False, text="hello", audio_waveform=_AUDIO.copy()),
            ScriptedTick(is_listen=False, text="world", audio_waveform=_AUDIO.copy()),
            ScriptedTick(is_listen=True),
            ScriptedTick(is_listen=True),
        ],
        name="A",
    )
    fake_b = ScriptedDuplexSession(
        ticks=[
            ScriptedTick(is_listen=True),
            ScriptedTick(is_listen=False, text="STOP", audio_waveform=_AUDIO.copy()),
            ScriptedTick(is_listen=False, text="goes", audio_waveform=_AUDIO.copy()),
            ScriptedTick(is_listen=False, text="on",   audio_waveform=_AUDIO.copy()),
        ],
        name="B",
    )

    trace = DebateOrchestrator(
        "test motion",
        sessions={"A": fake_a, "B": fake_b},
        listen_prob_scale={"A": 1.0, "B": 1.0},
        k_grace=0,
        silence_terminate_ticks=5,
        t_max=0,
        max_ticks=4,
        moderator_seed_audio=np.zeros(CHUNK_SAMPLES, np.float32),
        rng_seed=0,
    ).run()

    # Collision resolved at t=1 with rollback on A
    assert trace.ticks[1].rolled_back_this_tick == ["A"]

    # Break fires on A at t=2 — A's prefill at t=2 must NOT have received text_list
    # (the modeling-side guard would have discarded it anyway).
    assert "A" in trace.ticks[2].break_fired_this_tick
    assert fake_a.prefill_text_history[2] is None

    # B's barge text ("STOP") was deferred and injected into A's prefill at t=3
    assert fake_a.prefill_text_history[3] == ["STOP"]

    assert trace.metrics.rollbacks_performed == 1
    assert trace.metrics.barge_text_signals_sent == 1
