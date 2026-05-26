import numpy as np

from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
from tests._debate_fakes import ScriptedDuplexSession, ScriptedTick

_SPEAK_TICK = ScriptedTick(
    is_listen=False,
    text="hello",
    audio_waveform=np.ones(CHUNK_SAMPLES, dtype=np.float32),
    end_of_turn=False,
)


def _make_session():
    return ScriptedDuplexSession(ticks=[_SPEAK_TICK] * 5, name="A")


def test_break_lifecycle():
    session = _make_session()

    # t=0: normal speak tick
    session.prefill(np.zeros(CHUNK_SAMPLES, dtype=np.float32))
    r0 = session.generate(listen_prob_scale=1.0)
    assert r0.is_listen is False
    assert r0.text == "hello"
    assert r0.audio_waveform.shape == (CHUNK_SAMPLES,)
    assert r0.end_of_turn is False

    # arm break
    session.set_break()
    assert session.is_break_set() is True

    # t=1: break fires — forced listen
    session.prefill(np.zeros(CHUNK_SAMPLES, dtype=np.float32))
    r1 = session.generate(listen_prob_scale=1.0)
    assert r1.is_listen is True
    assert r1.text == ""
    assert r1.audio_waveform.shape == (CHUNK_SAMPLES,)
    assert np.all(r1.audio_waveform == 0.0)
    assert r1.end_of_turn is True
    assert session.is_break_set() is False  # auto-cleared after fire

    # orchestrator safety clear — no exception
    session.clear_break()

    # t=2: resumes normal speak
    session.prefill(np.zeros(CHUNK_SAMPLES, dtype=np.float32))
    r2 = session.generate(listen_prob_scale=1.0)
    assert r2.is_listen is False
    assert r2.text == "hello"
