"""CLI driver — simple alternating two-MiniCPM-o debate (no barge-in).

Usage:
    python scripts/run_simple_alternating_debate.py \
        --motion "..." --side-a-stance "..." --side-b-stance "..." --total-turns 6
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a simple alternating two-MiniCPM-o debate.")
    p.add_argument("--motion", required=True)
    p.add_argument("--side-a-stance", required=True)
    p.add_argument("--side-b-stance", required=True)
    p.add_argument("--total-turns", type=int, default=8)
    p.add_argument("--max-turn-ticks", type=int, default=4)
    p.add_argument("--first-speaker", default="A")
    p.add_argument("--listen-prob-scale-speaking", type=float, default=0.5)
    p.add_argument("--listen-prob-scale-listening", type=float, default=10.0)
    p.add_argument("--load-mode", choices=["single", "dual"], default="dual")
    p.add_argument("--ref-voice-a", default="af_bella")
    p.add_argument("--ref-voice-b", default="am_michael")
    p.add_argument("--out-dir", type=Path, default=None)
    return p.parse_args()


def _setup_logging(out_dir: Path) -> logging.Logger:
    log = logging.getLogger("simple_debate")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(out_dir / "run.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def _render_moderator_audio(kokoro, text: str, *, chunk_samples: int) -> np.ndarray:
    from companion_harness.debate.debate_artifacts import _resample_24k_to_16k

    samples, sr = kokoro.create(text, voice="af_bella", speed=1.0, lang="en-us")
    assert sr == 24000
    samples = np.asarray(samples, dtype=np.float32)
    resampled = _resample_24k_to_16k(samples)
    if len(resampled) < chunk_samples:
        resampled = np.pad(resampled, (0, chunk_samples - len(resampled)))
    else:
        resampled = resampled[:chunk_samples]
    return resampled.astype(np.float32)


def main() -> None:
    args = _parse_args()

    if args.out_dir is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        args.out_dir = _ROOT / "artifacts" / "debate" / f"simple_{ts}"
    args.out_dir.mkdir(parents=True, exist_ok=True)

    log = _setup_logging(args.out_dir)
    log.info("out_dir=%s load_mode=%s", args.out_dir, args.load_mode)

    log.info("Loading Kokoro TTS pipeline …")
    from kokoro_onnx import Kokoro

    _MODEL_PATH = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
    _VOICES_PATH = "/raid/yid042/models/kokoro/voices.json"
    kokoro = Kokoro(_MODEL_PATH, _VOICES_PATH)

    available_voices = set(kokoro.voices) if hasattr(kokoro, "voices") else set()
    if available_voices:
        for voice in (args.ref_voice_a, args.ref_voice_b):
            if voice not in available_voices:
                log.error("Kokoro voice %r not available. Available: %s", voice, sorted(available_voices))
                sys.exit(1)
    log.info("Kokoro loaded.")

    log.info("Loading MiniCPM-o model(s) (load_mode=%s) …", args.load_mode)
    t0 = time.monotonic()
    import torch
    from transformers import AutoModel

    MODEL_ID = "openbmb/MiniCPM-o-4_5"
    if args.load_mode == "single":
        base_model_a = AutoModel.from_pretrained(
            MODEL_ID, trust_remote_code=True, torch_dtype=torch.bfloat16
        ).to("cuda")
        base_model_a.eval()
        base_model_b = base_model_a
    else:
        base_model_a = AutoModel.from_pretrained(
            MODEL_ID, trust_remote_code=True, torch_dtype=torch.bfloat16
        ).to("cuda")
        base_model_a.eval()
        base_model_b = AutoModel.from_pretrained(
            MODEL_ID, trust_remote_code=True, torch_dtype=torch.bfloat16
        ).to("cuda")
        base_model_b.eval()
    log.info("Model load took %.1fs", time.monotonic() - t0)

    from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
    from companion_harness.debate.prompts import MODERATOR_OPENING, build_system_prompt_simple

    opening_text = MODERATOR_OPENING.format(motion=args.motion)
    seed_audio = _render_moderator_audio(kokoro, opening_text, chunk_samples=CHUNK_SAMPLES)
    log.info("Moderator seed audio rendered.")

    from companion_harness.debate.minicpm_duplex_session import MiniCPMDuplexSession

    sys_a = build_system_prompt_simple(
        name="A", opp_name="B", motion=args.motion, side="Proposition", stance=args.side_a_stance
    )
    sys_b = build_system_prompt_simple(
        name="B", opp_name="A", motion=args.motion, side="Opposition", stance=args.side_b_stance
    )

    session_a = MiniCPMDuplexSession(
        base_model_a,
        prefix_system_prompt=sys_a,
        ref_audio=None,
        force_listen_count=0,
        name="A",
    )
    session_b = MiniCPMDuplexSession(
        base_model_b,
        prefix_system_prompt=sys_b,
        ref_audio=None,
        force_listen_count=0,
        name="B",
    )
    log.info("Sessions ready.")

    from companion_harness.debate.simple_alternating_orchestrator import SimpleAlternatingOrchestrator

    orch = SimpleAlternatingOrchestrator(
        motion=args.motion,
        sessions={"A": session_a, "B": session_b},
        first_speaker=args.first_speaker,
        max_turn_ticks=args.max_turn_ticks,
        total_turns=args.total_turns,
        listen_prob_scale_speaking=args.listen_prob_scale_speaking,
        listen_prob_scale_listening=args.listen_prob_scale_listening,
        moderator_seed_audio=seed_audio,
    )

    log.info(
        "Starting simple debate: motion=%r total_turns=%d max_turn_ticks=%d",
        args.motion, args.total_turns, args.max_turn_ticks,
    )
    trace = orch.run()

    natural_eot_count = sum(1 for t in trace.turns if t.natural_eot)
    for turn in trace.turns:
        preview = turn.text[:80] + ("..." if len(turn.text) > 80 else "")
        print(f"[turn {turn.turn_idx} {turn.speaker}] {preview}")

    from companion_harness.debate.debate_artifacts import (
        render_audio_from_simple_trace,
        write_simple_transcript,
    )

    transcript_path = args.out_dir / "transcript.json"
    wav_path = args.out_dir / "debate.wav"
    write_simple_transcript(trace, transcript_path)
    render_audio_from_simple_trace(
        trace,
        kokoro_pipeline=kokoro,
        voice_for={"A": args.ref_voice_a, "B": args.ref_voice_b},
        out_wav=wav_path,
    )

    print("\n=== Simple debate complete ===")
    print(f"  total_turns_completed : {len(trace.turns)}")
    print(f"  total_ticks           : {trace.total_ticks}")
    print(f"  natural_eot_count     : {natural_eot_count}")
    print(f"  transcript            : {transcript_path}")
    print(f"  audio                 : {wav_path}")


if __name__ == "__main__":
    main()
