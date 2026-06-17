"""Hermetic tests for the gpt-realtime-2 Realtime-WS adapter + the runner label fix.

No WebSocket: a fake transport records the out-of-band response payload and returns
a canned token. The real WS path is exercised by scripts/probe_gpt_realtime.py.
"""
import base64
import importlib.util
import json
from pathlib import Path

import numpy as np

from companion_harness.foreground_model_gpt_realtime import (
    GptRealtimeModel,
    _audio_msg,
    _pcm16_24k_b64,
    _reduce_text,
    _response_payload,
    _text_msg,
)


class _FakeTransport:
    def __init__(self, reply="NOW_BRIEF"):
        self.reply = reply
        self.payloads = []

    def request(self, payload):
        self.payloads.append(payload)
        return self.reply


def _model(reply="NOW_BRIEF"):
    fake = _FakeTransport(reply)
    return GptRealtimeModel(transport_factory=lambda: fake), fake


def test_pcm16_24k_b64_resamples_to_24k_raw_pcm():
    audio = (0.5 * np.sin(2 * np.pi * 220 * np.arange(16000) / 16000)).astype(np.float32)
    b64 = _pcm16_24k_b64(audio, src_rate=16000)
    raw = base64.b64decode(b64)
    assert raw[:4] != b"RIFF"                 # raw PCM, NOT a WAV container
    samples = np.frombuffer(raw, dtype="<i2")
    assert abs(len(samples) - 24000) <= 2     # 1.0 s @ 16k -> ~24000 @ 24k
    assert samples.dtype == np.dtype("<i2")


def test_chat_builds_text_response_payload():
    model, fake = _model("WAIT")
    out = model.chat("decide: NOW or WAIT?", max_new_tokens=6)
    assert out == "WAIT"
    (p,) = fake.payloads
    assert p["type"] == "response.create"
    r = p["response"]
    assert r["conversation"] == "none"
    assert r["output_modalities"] == ["text"]
    assert r["max_output_tokens"] == 6 + 80  # caller budget + reasoning headroom
    assert r["input"] == [_text_msg("decide: NOW or WAIT?")]
    assert "instructions" not in r            # text arms bake the prompt into the user turn


def test_chat_audio_builds_audio_payload_with_instructions():
    model, fake = _model("now-brief")
    out = model.chat_audio(np.zeros(1600, dtype=np.float32), system_prompt="policy here", max_new_tokens=6)
    assert out == "now-brief"
    (p,) = fake.payloads
    r = p["response"]
    assert r["instructions"] == "policy here"
    content = r["input"][0]["content"][0]
    assert content["type"] == "input_audio"
    base64.b64decode(content["audio"])        # valid base64


def test_chat_audio_without_system_prompt_omits_instructions():
    model, fake = _model()
    model.chat_audio(np.zeros(800, dtype=np.float32), system_prompt="")
    assert "instructions" not in fake.payloads[0]["response"]


def test_reduce_text_handles_delta_done_error():
    assert _reduce_text({"type": "response.output_text.delta", "delta": "WA"}) == ("WA", False, None)
    assert _reduce_text({"type": "response.done"}) == ("", True, None)
    d, done, err = _reduce_text({"type": "error", "error": {"message": "boom"}})
    assert done and err and "boom" in err
    assert _reduce_text({"type": "session.updated"}) == ("", False, None)  # ignored


def test_response_payload_shape():
    p = _response_payload([_audio_msg("QUJD")], instructions="sys", max_tokens=12)
    assert p["response"]["input"][0]["content"][0] == {"type": "input_audio", "audio": "QUJD"}
    assert p["response"]["instructions"] == "sys"
    assert p["response"]["max_output_tokens"] == 12


# --- runner model-label fix (R2): --render-only must not mislabel a GPT run ----

def _load_runner():
    path = Path(__file__).resolve().parent.parent / "scripts" / "run_tact_layer3.py"
    spec = importlib.util.spec_from_file_location("run_tact_layer3", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_read_meta_prefers_persisted_label(tmp_path):
    runner = _load_runner()
    (tmp_path / "meta.json").write_text(json.dumps({"model": "openai/gpt-realtime-2", "date": "2026-05-23"}))
    meta = runner._read_meta(tmp_path, model_flag="minicpm")  # flag says minicpm...
    assert meta["model"] == "openai/gpt-realtime-2"           # ...but persisted label wins


def test_read_meta_falls_back_to_flag_label(tmp_path):
    runner = _load_runner()
    assert runner._read_meta(tmp_path, "gpt-realtime-2")["model"] == "openai/gpt-realtime-2"
    assert runner._read_meta(tmp_path, "minicpm")["model"] == "openbmb/MiniCPM-o-4_5"
