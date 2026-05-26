"""Debate artifact writers — transcript JSON and Kokoro WAV stitcher."""

from __future__ import annotations

import dataclasses
import json
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from companion_harness.debate.debate_orchestrator import DebateTrace

if TYPE_CHECKING:
    pass


def write_transcript(trace: DebateTrace, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    d = dataclasses.asdict(trace)
    with out_path.open("w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


def render_audio_from_transcript(
    trace: DebateTrace,
    *,
    kokoro_pipeline,
    voice_for: dict[str, str],
    out_wav: Path,
) -> None:
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    SR = 24000
    GAP = np.zeros(int(0.3 * SR), dtype=np.float32)

    # collect speak-runs: maximal contiguous ticks where exactly one speaker is audible
    runs: list[tuple[str, list[str]]] = []  # (speaker, [text_chunks])
    for tick in trace.ticks:
        if len(tick.audible) != 1:
            runs.append(("", []))  # sentinel for boundary
            continue
        speaker = tick.audible[0]
        text = tick.per_speaker[speaker]["text"]
        if runs and runs[-1][0] == speaker:
            runs[-1][1].append(text)
        else:
            runs.append((speaker, [text]))

    # filter out sentinels and empty runs
    speak_runs = [(sp, chunks) for sp, chunks in runs if sp and any(chunks)]

    if not speak_runs:
        pcm = np.zeros(SR, dtype=np.int16)
        _write_wav(out_wav, pcm, SR)
        return

    segments: list[np.ndarray] = []
    for i, (speaker, chunks) in enumerate(speak_runs):
        text = " ".join(c for c in chunks if c)
        if not text:
            continue
        samples, sr = kokoro_pipeline.create(
            text, voice=voice_for[speaker], speed=1.0, lang="en-us"
        )
        assert sr == SR
        samples = np.asarray(samples, dtype=np.float32)
        if i > 0:
            segments.append(GAP.copy())
        segments.append(samples)

    if not segments:
        pcm = np.zeros(SR, dtype=np.int16)
        _write_wav(out_wav, pcm, SR)
        return

    stitched = np.concatenate(segments)
    pcm = (np.clip(stitched, -1.0, 1.0) * 32767).astype(np.int16)
    _write_wav(out_wav, pcm, SR)


def _write_wav(path: Path, pcm: np.ndarray, framerate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(pcm.tobytes())


def _resample_24k_to_16k(audio: np.ndarray) -> np.ndarray:
    try:
        from scipy.signal import resample_poly
        return resample_poly(audio, up=2, down=3).astype(np.float32)
    except ImportError:
        return np.repeat(audio, 2)[::3].astype(np.float32)
