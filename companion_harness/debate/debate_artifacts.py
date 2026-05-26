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
    from companion_harness.debate.simple_alternating_orchestrator import SimpleDebateTrace


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
    """Render the text trajectory through Kokoro, preserving collision-tick overlap.

    Per-speaker contiguous-tick runs are concatenated and rendered as one Kokoro
    call each, then placed on the timeline at `start_tick * 1s`. The per-speaker
    tracks are summed, so when the transcript shows both speakers audible on the
    same tick their voices play simultaneously in the mix.

    A speaker's own consecutive runs cannot overlap themselves: if a run's
    natural Kokoro duration spills past the next same-speaker run's start tick,
    the later run is pushed out with a 200 ms gap.
    """
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    SR = 24000
    TICK_SAMPLES = SR  # 1 s/tick

    # Build per-speaker contiguous-tick runs from the transcript.
    per_speaker_runs: dict[str, list[tuple[int, list[str]]]] = {n: [] for n in voice_for}
    for tick in trace.ticks:
        for name in voice_for:
            sp = tick.per_speaker.get(name)
            if sp is None or sp["is_listen"] or not sp["text"]:
                continue
            runs = per_speaker_runs[name]
            # contiguous = same speaker on next tick (len(chunks) chunks spans len(chunks) ticks)
            if runs and runs[-1][0] + len(runs[-1][1]) == tick.tick:
                runs[-1][1].append(sp["text"])
            else:
                runs.append((tick.tick, [sp["text"]]))

    # Render each run; place on the timeline at start_tick * TICK_SAMPLES with
    # per-speaker no-self-overlap. Collect (offset_samples, ndarray) tuples.
    placements: list[tuple[int, np.ndarray]] = []
    for name, runs in per_speaker_runs.items():
        next_min_offset = 0
        for start_tick, chunks in runs:
            text = " ".join(chunks)
            samples, sr = kokoro_pipeline.create(
                text, voice=voice_for[name], speed=1.0, lang="en-us"
            )
            assert sr == SR
            samples = np.asarray(samples, dtype=np.float32)
            offset = max(start_tick * TICK_SAMPLES, next_min_offset)
            placements.append((offset, samples))
            next_min_offset = offset + len(samples) + int(0.2 * SR)

    if not placements:
        _write_wav(out_wav, np.zeros(SR, dtype=np.int16), SR)
        return

    total_samples = max(off + len(s) for off, s in placements)
    mix = np.zeros(total_samples, dtype=np.float32)
    for off, samples in placements:
        mix[off : off + len(samples)] += samples

    pcm = (np.clip(mix, -1.0, 1.0) * 32767).astype(np.int16)
    _write_wav(out_wav, pcm, SR)


def _write_wav(path: Path, pcm: np.ndarray, framerate: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(framerate)
        wf.writeframes(pcm.tobytes())


def write_simple_transcript(trace: "SimpleDebateTrace", out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    d = dataclasses.asdict(trace)
    with out_path.open("w") as f:
        json.dump(d, f, indent=2, sort_keys=True)


def render_audio_from_simple_trace(
    trace: "SimpleDebateTrace",
    *,
    kokoro_pipeline,
    voice_for: dict[str, str],
    out_wav: Path,
    inter_turn_gap_s: float = 0.4,
) -> None:
    """One Kokoro call per turn (natural-pacing); concatenate sequentially with a gap."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    SR = 24000
    gap = np.zeros(int(inter_turn_gap_s * SR), dtype=np.float32)
    segments: list[np.ndarray] = []
    for turn in trace.turns:
        if not turn.text:
            continue
        samples, sr = kokoro_pipeline.create(
            turn.text, voice=voice_for[turn.speaker], speed=1.0, lang="en-us"
        )
        assert sr == SR
        segments.append(np.asarray(samples, dtype=np.float32))
        segments.append(gap)
    if not segments:
        _write_wav(out_wav, np.zeros(SR, dtype=np.int16), SR)
        return
    mix = np.concatenate(segments)
    pcm = (np.clip(mix, -1.0, 1.0) * 32767).astype(np.int16)
    _write_wav(out_wav, pcm, SR)


def _resample_24k_to_16k(audio: np.ndarray) -> np.ndarray:
    try:
        from scipy.signal import resample_poly
        return resample_poly(audio, up=2, down=3).astype(np.float32)
    except ImportError:
        return np.repeat(audio, 2)[::3].astype(np.float32)
