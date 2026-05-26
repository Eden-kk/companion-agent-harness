"""CLI driver — two-MiniCPM-o debate harness.

Usage:
    python scripts/run_two_minicpm_debate.py \
        --motion "..." --side-a-stance "..." --side-b-stance "..." --max-ticks 30
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Resolve worktree root so relative paths work when invoked from anywhere.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a two-MiniCPM-o debate.")
    p.add_argument("--motion", required=True)
    p.add_argument("--side-a-stance", required=True)
    p.add_argument("--side-b-stance", required=True)
    p.add_argument("--max-ticks", type=int, default=30)
    p.add_argument("--k-grace", type=int, default=1)
    p.add_argument("--n-deadlock", type=int, default=4)
    p.add_argument("--t-max", type=int, default=30)
    p.add_argument("--load-mode", choices=["single", "dual"], default=None)
    p.add_argument("--listen-prob-scale-a", type=float, default=0.9)
    p.add_argument("--listen-prob-scale-b", type=float, default=0.9)
    p.add_argument("--ref-voice-a", default="af_bella")
    p.add_argument("--ref-voice-b", default="am_michael")
    p.add_argument("--out-dir", type=Path, default=None)
    p.add_argument("--rng-seed", type=int, default=0)
    return p.parse_args()


def _load_stage0_verdict(root: Path) -> dict:
    verdict_path = root / "artifacts" / "stage0_verdict.json"
    if not verdict_path.exists():
        print(
            "ERROR: Stage 0 has not been run; pass --load-mode explicitly or run "
            "scripts/probe_minicpm_dual_duplex.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    with verdict_path.open() as f:
        return json.load(f)


def _setup_logging(out_dir: Path) -> logging.Logger:
    log = logging.getLogger("debate")
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(out_dir / "run.log")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    return log


def _render_moderator_audio(
    kokoro, text: str, *, chunk_samples: int
) -> np.ndarray:
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


def _run_debate(
    *,
    motion: str,
    session_a,
    session_b,
    seed_audio: np.ndarray,
    nudge_audio: np.ndarray,
    args: argparse.Namespace,
    listen_prob_scale: dict[str, float],
) -> "DebateTrace":
    from companion_harness.debate.debate_orchestrator import DebateOrchestrator

    orch = DebateOrchestrator(
        motion=motion,
        sessions={"A": session_a, "B": session_b},
        listen_prob_scale=listen_prob_scale,
        k_grace=args.k_grace,
        n_deadlock=args.n_deadlock,
        t_max=args.t_max,
        max_ticks=args.max_ticks,
        moderator_seed_audio=seed_audio,
        moderator_nudge_audio=nudge_audio,
        rng_seed=args.rng_seed,
    )
    return orch.run()


def _write_artifacts(trace, out_dir: Path, args: argparse.Namespace, kokoro) -> None:
    from companion_harness.debate import debate_artifacts

    debate_artifacts.write_transcript(trace, out_dir / "transcript.json")
    debate_artifacts.render_audio_from_transcript(
        trace,
        kokoro_pipeline=kokoro,
        voice_for={"A": args.ref_voice_a, "B": args.ref_voice_b},
        out_wav=out_dir / "debate.wav",
    )


def _smoke_check(trace, log: logging.Logger) -> bool:
    assert trace.metrics.total_ticks > 0, "total_ticks must be > 0"
    assert trace.metrics.max_collision_duration_ticks <= trace.k_grace + 1, (
        f"max_collision_duration_ticks={trace.metrics.max_collision_duration_ticks} "
        f"> k_grace+1={trace.k_grace + 1}"
    )
    if trace.metrics.barge_in_successes >= 1:
        log.info("SMOKE CHECK PASS: barge_in_successes=%d", trace.metrics.barge_in_successes)
        return True
    log.warning("barge_in_successes=0 — will retry with listen_prob_scale=0.7")
    return False


def main() -> None:
    args = _parse_args()

    # Step 1: resolve load_mode, create out_dir, configure logging
    verdict = _load_stage0_verdict(_ROOT)
    if args.load_mode is None:
        args.load_mode = verdict["load_mode"]
    plan_b_engaged = verdict.get("plan_b_engaged", False)

    if args.out_dir is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        args.out_dir = _ROOT / "artifacts" / "debate" / ts
    args.out_dir.mkdir(parents=True, exist_ok=True)

    log = _setup_logging(args.out_dir)
    log.info("load_mode=%s plan_b_engaged=%s out_dir=%s", args.load_mode, plan_b_engaged, args.out_dir)

    # Step 2: load Kokoro
    log.info("Loading Kokoro TTS pipeline …")
    from kokoro_onnx import Kokoro

    _MODEL_PATH = "/raid/yid042/models/kokoro/kokoro-v0_19.onnx"
    _VOICES_PATH = "/raid/yid042/models/kokoro/voices.json"
    kokoro = Kokoro(_MODEL_PATH, _VOICES_PATH)

    # Preflight: verify requested voices exist
    available_voices = set(kokoro.voices) if hasattr(kokoro, "voices") else set()
    if available_voices:
        for voice in (args.ref_voice_a, args.ref_voice_b):
            if voice not in available_voices:
                log.error("Kokoro voice %r not available. Available: %s", voice, sorted(available_voices))
                sys.exit(1)
    log.info("Kokoro loaded.")

    # Step 3: load MiniCPM base model(s)
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

    try:
        alloc_gb = torch.cuda.memory_allocated() / 1e9
        log.info("VRAM allocated after load: %.2f GB", alloc_gb)
    except Exception:
        pass

    # Step 4: render moderator seed audio via Kokoro
    log.info("Rendering moderator audio …")
    from companion_harness.debate.minicpm_duplex_session import CHUNK_SAMPLES
    from companion_harness.debate.prompts import (
        MODERATOR_NUDGE,
        MODERATOR_OPENING,
        build_system_prompt,
    )

    opening_text = MODERATOR_OPENING.format(motion=args.motion)
    seed_audio = _render_moderator_audio(kokoro, opening_text, chunk_samples=CHUNK_SAMPLES)
    nudge_audio = _render_moderator_audio(kokoro, MODERATOR_NUDGE, chunk_samples=CHUNK_SAMPLES)
    log.info("Moderator audio rendered.")

    # Step 5: build MiniCPMDuplexSession instances
    from companion_harness.debate.minicpm_duplex_session import MiniCPMDuplexSession

    sys_a = build_system_prompt(
        name="A", opp_name="B", motion=args.motion, side="Proposition", stance=args.side_a_stance
    )
    sys_b = build_system_prompt(
        name="B", opp_name="A", motion=args.motion, side="Opposition", stance=args.side_b_stance
    )

    log.info("Preparing duplex sessions …")
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

    # Step 6: self-voice pre-check SKIPPED
    # TODO: pre-check skipped per first-iteration decision; re-enable if quality is poor

    # Step 7–9: run debate + write artifacts
    log.info("Starting debate: motion=%r max_ticks=%d", args.motion, args.max_ticks)
    listen_scale = {"A": args.listen_prob_scale_a, "B": args.listen_prob_scale_b}
    trace = _run_debate(
        motion=args.motion,
        session_a=session_a,
        session_b=session_b,
        seed_audio=seed_audio,
        nudge_audio=nudge_audio,
        args=args,
        listen_prob_scale=listen_scale,
    )

    # Print per-tick progress summary
    for rec in trace.ticks:
        a_verb = "speak" if not rec.per_speaker.get("A", {}).get("is_listen", True) else "listen"
        b_verb = "speak" if not rec.per_speaker.get("B", {}).get("is_listen", True) else "listen"
        print(f"[tick {rec.tick:3d}] A: {a_verb}, B: {b_verb}, audible={rec.audible}")

    log.info("Debate finished: total_ticks=%d", trace.metrics.total_ticks)

    log.info("Writing artifacts to %s …", args.out_dir)
    _write_artifacts(trace, args.out_dir, args, kokoro)
    log.info("Artifacts written.")

    # Step 10: smoke check
    passed = _smoke_check(trace, log)

    if not passed:
        # Retry with lower listen_prob_scale
        log.info("Retry: resetting sessions and rebuilding orchestrator with listen_prob_scale=0.7")
        retry_dir = Path(str(args.out_dir) + "_retry")
        retry_dir.mkdir(parents=True, exist_ok=True)

        session_a.reset(prefix_system_prompt=sys_a)
        session_b.reset(prefix_system_prompt=sys_b)

        retry_scale = {"A": 0.7, "B": 0.7}
        trace = _run_debate(
            motion=args.motion,
            session_a=session_a,
            session_b=session_b,
            seed_audio=seed_audio,
            nudge_audio=nudge_audio,
            args=args,
            listen_prob_scale=retry_scale,
        )

        _write_artifacts(trace, retry_dir, args, kokoro)
        passed = _smoke_check(trace, log)

        if not passed:
            log.error(
                "FAIL after retry: barge_in_successes=%d total_ticks=%d "
                "collision_count=%d deadlocks=%d",
                trace.metrics.barge_in_successes,
                trace.metrics.total_ticks,
                trace.metrics.collision_count,
                trace.metrics.deadlocks_detected,
            )
            sys.exit(1)

    # Step 11: final summary
    m = trace.metrics
    log.info(
        "FINAL METRICS: total_ticks=%d collision_count=%d barge_in_successes=%d "
        "harness_forced_breaks=%d self_yields=%d deadlocks=%d moderator_nudges=%d",
        m.total_ticks, m.collision_count, m.barge_in_successes,
        m.harness_forced_breaks, m.self_yields, m.deadlocks_detected,
        m.moderator_nudges_fired,
    )
    print("\n=== Debate complete ===")
    print(f"  transcript : {args.out_dir / 'transcript.json'}")
    print(f"  audio      : {args.out_dir / 'debate.wav'}")
    print(f"  log        : {args.out_dir / 'run.log'}")
    print(f"  total_ticks            : {m.total_ticks}")
    print(f"  collision_count        : {m.collision_count}")
    print(f"  barge_in_successes     : {m.barge_in_successes}")
    print(f"  harness_forced_breaks  : {m.harness_forced_breaks}")
    print(f"  self_yields            : {m.self_yields}")
    print(f"  deadlocks_detected     : {m.deadlocks_detected}")
    print(f"  load_mode              : {args.load_mode}")


if __name__ == "__main__":
    main()
