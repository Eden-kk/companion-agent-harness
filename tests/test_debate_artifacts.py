"""Stage 4 artifact tests — transcript JSON schema + Kokoro WAV stitcher.

Voices used: af_bella (speaker A), am_michael (speaker B).
af_heart is not present in the kokoro-v0_19.onnx voices bundle on this host.
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from companion_harness.debate.debate_orchestrator import (
    DebateMetrics,
    DebateTrace,
    TickRecord,
)
from companion_harness.debate.debate_artifacts import render_audio_from_transcript, write_transcript

pytestmark = pytest.mark.gpu

_MODEL_PATH = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
_VOICES_PATH = "/raid/yid042/models/kokoro/voices.json"


def _make_trace() -> DebateTrace:
    """Fake trace: A speaks ticks 0-1, B speaks tick 2."""
    ticks = [
        TickRecord(
            tick=0,
            audible=["A"],
            floor="A",
            break_fired_this_tick=[],
            break_armed_for_next_tick=[],
            silence_run=0,
            moderator_nudge_fired=False,
            per_speaker={
                "A": {"is_listen": False, "text": "hello", "end_of_turn": False, "current_time": 0.0},
                "B": {"is_listen": True, "text": "", "end_of_turn": False, "current_time": 0.0},
            },
        ),
        TickRecord(
            tick=1,
            audible=["A"],
            floor="A",
            break_fired_this_tick=[],
            break_armed_for_next_tick=[],
            silence_run=0,
            moderator_nudge_fired=False,
            per_speaker={
                "A": {"is_listen": False, "text": "world", "end_of_turn": True, "current_time": 0.0},
                "B": {"is_listen": True, "text": "", "end_of_turn": False, "current_time": 0.0},
            },
        ),
        TickRecord(
            tick=2,
            audible=["B"],
            floor="B",
            break_fired_this_tick=[],
            break_armed_for_next_tick=[],
            silence_run=0,
            moderator_nudge_fired=False,
            per_speaker={
                "A": {"is_listen": True, "text": "", "end_of_turn": False, "current_time": 0.0},
                "B": {"is_listen": False, "text": "goodbye", "end_of_turn": True, "current_time": 0.0},
            },
        ),
    ]
    metrics = DebateMetrics(total_ticks=3, turn_count_per_speaker={"A": 1, "B": 1})
    return DebateTrace(motion="test motion", ticks=ticks, metrics=metrics, k_grace=1, t_max=30, n_deadlock=4)


def test_write_transcript(tmp_path: Path) -> None:
    trace = _make_trace()
    out = tmp_path / "transcript.json"
    write_transcript(trace, out)

    import json
    data = json.loads(out.read_text())

    assert data["motion"] == "test motion"
    assert data["k_grace"] == 1
    assert data["t_max"] == 30
    assert data["n_deadlock"] == 4
    assert "metrics" in data
    assert "ticks" in data
    assert len(data["ticks"]) == 3
    assert data["ticks"][0]["audible"] == ["A"]
    assert data["ticks"][2]["audible"] == ["B"]
    assert "per_speaker" in data["ticks"][0]


def test_render_audio_from_transcript(tmp_path: Path) -> None:
    from kokoro_onnx import Kokoro

    kokoro = Kokoro(_MODEL_PATH, _VOICES_PATH)
    trace = _make_trace()
    out = tmp_path / "debate.wav"

    render_audio_from_transcript(
        trace,
        kokoro_pipeline=kokoro,
        voice_for={"A": "af_bella", "B": "am_michael"},
        out_wav=out,
    )

    assert out.exists()
    with wave.open(str(out), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getframerate() == 24000
        n_frames = wf.getnframes()
    assert n_frames > 24000 * 0.5  # > 0.5 seconds
