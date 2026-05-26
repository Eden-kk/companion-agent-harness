"""Design §6 test 2 — acoustic cross-feed on collision tick."""

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateOrchestrator
from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_AUDIO_A = np.ones(CHUNK_SAMPLES, dtype=np.float32)
_AUDIO_B = np.full(CHUNK_SAMPLES, 0.5, dtype=np.float32)


def test_acoustic_overlap():
    fake_a = ScriptedDuplexSession(
        ticks=[
            ScriptedTick(is_listen=False, audio_waveform=_AUDIO_A.copy()),
            ScriptedTick(is_listen=True),
        ],
        name="A",
    )
    fake_b = ScriptedDuplexSession(
        ticks=[
            ScriptedTick(is_listen=False, audio_waveform=_AUDIO_B.copy()),
            ScriptedTick(is_listen=True),
        ],
        name="B",
    )

    trace = DebateOrchestrator(
        "test motion",
        sessions={"A": fake_a, "B": fake_b},
        listen_prob_scale={"A": 1.0, "B": 1.0},
        k_grace=1,
        n_deadlock=4,
        t_max=0,
        max_ticks=2,
        moderator_seed_audio=np.zeros(CHUNK_SAMPLES, np.float32),
        moderator_nudge_audio=np.full(CHUNK_SAMPLES, 0.7, np.float32),
        rng_seed=0,
    ).run()

    # At t=1 each fake's prefill received the OTHER's t=0 audio
    assert np.allclose(fake_a.prefill_history[1], _AUDIO_B), "A should have received B's audio"
    assert np.allclose(fake_b.prefill_history[1], _AUDIO_A), "B should have received A's audio"

    assert trace.metrics.collision_count == 1
    assert trace.metrics.max_collision_duration_ticks == 1
