"""PR7 — audio-mode (input_mode='audio') parity.

Hermetic: fake model + no Kokoro (kokoro_factory returns None → silence audio), so
the audio timeline + RMS speaking-mask path runs without TTS or GPU. Confirms the
audio branch produces the same ReplayRun shape as text mode.
"""
import asyncio

from companion_harness.evals.adapters.tact_bench import TactCaseSource, TactMiniCPMDriver


class _FakeDuplex:
    def __init__(self):
        self.model = self
        self.audio_lens: list[int] = []

    def reset_session(self, reset_token2wav_cache=True):
        pass

    def prepare(self, prefix_system_prompt=None):
        pass

    def streaming_prefill(self, audio_waveform=None, text_list=None):
        self.audio_lens.append(0 if audio_waveform is None else len(audio_waveform))
        return {"success": True}

    def streaming_generate(self, **kwargs):
        return {"is_listen": True, "text": ""}


class _FakeModel:
    def __init__(self):
        self._duplex = _FakeDuplex()


class _RC:
    def __init__(self, output_dir):
        self.output_dir = output_dir


def _case(case_id):
    return next(c for c in TactCaseSource(input_mode="audio").iter_cases("all") if c.case_id == case_id)


def test_audio_mode_runs_and_feeds_audio_chunks(tmp_path):
    case = _case("TC1-defer")  # t_available=3
    fake = _FakeModel()
    driver = TactMiniCPMDriver(
        input_mode="audio", model_factory=lambda: fake, kokoro_factory=lambda: None,  # silence
    )
    rr = asyncio.run(driver.run(case, None, _RC(tmp_path)))

    assert rr.final_status == "completed"
    assert rr.results["input_mode"] == "audio"
    traj = rr.results["trajectory"]
    assert [c["t"] for c in traj if c["injected"]] == [3]
    # every chunk fed a full 1s audio frame (silence, since no Kokoro)
    assert fake._duplex.audio_lens and all(n == 16000 for n in fake._duplex.audio_lens)
    # speaking mask present, same length as trajectory
    assert len(rr.results["speaking_mask"]) == len(traj)
    # event log written with the bookkeeping trio
    rows = (rr.event_log_path).read_text().splitlines()
    types = {__import__("json").loads(r)["event_type"] for r in rows}
    assert {"benchmark_case_started", "held_result_injected", "benchmark_case_completed"} <= types


def test_unknown_input_mode_rejected(tmp_path):
    import pytest

    driver = TactMiniCPMDriver(input_mode="video", model_factory=_FakeModel)
    with pytest.raises(NotImplementedError):
        asyncio.run(driver.run(_case("TC1-defer"), None, _RC(tmp_path)))
