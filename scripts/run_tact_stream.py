"""run_tact_stream.py — TACT-Bench streaming-trajectory orchestrator (S5).

CLI:
  python scripts/run_tact_stream.py --model minicpm [--arms vanilla_audio,prompted_audio,...] \
      [--cases TC1,TC2] [--limit N] [--k 3] [--out results/] [--realtime]

Frozen arm matrix (§9 S0-freeze):
  minicpm : vanilla_audio, prompted_audio, vanilla_text, prompted_text, monitor_stream
  gpt     : vanilla_audio, prompted_audio, monitor_stream

Output layout:
  out/<model>/<condition>/run<r>/<case>.json   raw emissions + per-case score
  out/<model>/<condition>/scores.json          per-run aggregate per condition
  out/<model>/<condition>/aggregate.json       mean±spread over k runs
  out/baselines.json                           oracle / always_silent / always_deliver
  out/meta.json                                run metadata
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ---------------------------------------------------------------------------
# Frozen default arms per model
# ---------------------------------------------------------------------------
_DEFAULT_ARMS = {
    "minicpm": ["vanilla_audio", "prompted_audio", "vanilla_text", "prompted_text", "monitor_stream"],
    "gpt-realtime-2": ["vanilla_audio", "prompted_audio", "monitor_stream"],
}

# Map condition name → (arm, input_modality) for native runners
# monitor_stream handled separately
_CONDITION_PARAMS: dict[str, tuple[str, str]] = {
    "vanilla_audio": ("vanilla", "audio"),
    "prompted_audio": ("prompted", "audio"),
    "vanilla_text": ("vanilla", "text"),
    "prompted_text": ("prompted", "text"),
}


def _parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="TACT-Bench streaming orchestrator")
    p.add_argument("--model", required=True, choices=["minicpm", "gpt-realtime-2"])
    p.add_argument("--arms", default=None,
                   help="Comma-separated condition names; default = frozen matrix")
    p.add_argument("--cases", default=None,
                   help="Comma-separated case IDs (e.g. TC1,TC2); default = all")
    p.add_argument("--limit", type=int, default=None,
                   help="Max number of cases to run (applied after --cases filter)")
    p.add_argument("--k", type=int, default=3, help="Repetitions per case (default 3)")
    p.add_argument("--out", default="results", help="Output root directory")
    p.add_argument("--realtime", action="store_true",
                   help="Enable real-time pacing for gpt (wall-clock 1× streaming)")
    p.add_argument("--render-only", action="store_true",
                   help="Re-score existing raw emissions without re-running")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# JSON serialisation helpers
# ---------------------------------------------------------------------------
def _emission_to_dict(em) -> dict:
    return dataclasses.asdict(em)


def _load_model_minicpm():
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel
    return MiniCPMStreamingModel()  # loads weights in __init__


def _load_model_gpt():
    # gpt runner uses a transport; no persistent model object to load
    return None


def _run_condition_minicpm(condition: str, case_ids: list[str], k: int, model) -> dict[str, list[list]]:
    """Returns {case_id: [[Emission, ...] * k]}"""
    from companion_harness.evals.adapters import minicpm_stream_runner
    from companion_harness.evals.adapters import monitor_stream_runner  # built in parallel

    results: dict[str, list[list]] = {}
    for cid in case_ids:
        runs: list[list] = []
        for _ in range(k):
            if condition == "monitor_stream":
                emissions = monitor_stream_runner.run_case(cid, model, "minicpm")
            else:
                arm, modality = _CONDITION_PARAMS[condition]
                emissions = minicpm_stream_runner.run_case(cid, arm, modality, model)
            runs.append(emissions)
        results[cid] = runs
    return results


def _run_condition_gpt(condition: str, case_ids: list[str], k: int, realtime: bool) -> dict[str, list[list]]:
    """Returns {case_id: [[Emission, ...] * k]}"""
    from companion_harness.evals.adapters import gpt_stream_runner
    from companion_harness.evals.adapters import monitor_stream_runner  # built in parallel

    gpt_model = None
    if condition == "monitor_stream":
        from companion_harness.foreground_model_gpt_realtime import GptRealtimeModel
        gpt_model = GptRealtimeModel()  # stateless out-of-band adapter for the non-native monitor arm

    results: dict[str, list[list]] = {}
    for cid in case_ids:
        runs: list[list] = []
        for _ in range(k):
            if condition == "monitor_stream":
                emissions = monitor_stream_runner.run_case(cid, gpt_model, "gpt")
            else:
                arm, _ = _CONDITION_PARAMS[condition]
                emissions = gpt_stream_runner.run_case(cid, arm, realtime=realtime)
            runs.append(emissions)
        results[cid] = runs
    return results


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _persist_run(out_dir: Path, case_id: str, r: int, emissions: list, per_case_score: dict) -> None:
    run_dir = out_dir / f"run{r}"
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "case_id": case_id,
        "emissions": [_emission_to_dict(e) for e in emissions],
        "score": per_case_score,
    }
    (run_dir / f"{case_id}.json").write_text(json.dumps(payload, indent=2))


def _persist_scores(out_dir: Path, r: int, run_score: dict) -> None:
    scores_path = out_dir / "scores.json"
    existing: list[dict] = []
    if scores_path.exists():
        existing = json.loads(scores_path.read_text())
    existing.append({"run": r, "metrics": run_score})
    scores_path.write_text(json.dumps(existing, indent=2))


def _load_existing_emissions(out_dir: Path, case_ids: list[str], k: int) -> dict[str, list[list]]:
    """Load persisted emissions for render-only mode."""
    from companion_harness.evals.adapters.tact_bench_stream_types import Emission

    results: dict[str, list[list]] = {}
    for cid in case_ids:
        runs: list[list] = []
        for r in range(1, k + 1):
            path = out_dir / f"run{r}" / f"{cid}.json"
            if path.exists():
                data = json.loads(path.read_text())
                emissions = [Emission(**e) for e in data["emissions"]]
            else:
                emissions = []
            runs.append(emissions)
        results[cid] = runs
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None) -> None:
    args = _parse_args(argv)

    from companion_harness.evals.adapters.tact_bench_layer3 import (
        load_layer3, score_run, aggregate_runs,
        oracle_run, always_silent_run, always_deliver_run,
        preregistered_config,
    )
    from companion_harness.evals.adapters.tact_bench import _ARMS
    from companion_harness.evals.adapters import delivery_detector

    cases = load_layer3()
    all_case_ids = [c.id for c in cases]

    if args.cases:
        wanted = {x.strip() for x in args.cases.split(",")}
        all_case_ids = [cid for cid in all_case_ids if cid in wanted]
    if args.limit:
        all_case_ids = all_case_ids[: args.limit]

    arms = (
        [a.strip() for a in args.arms.split(",")]
        if args.arms
        else _DEFAULT_ARMS[args.model]
    )

    out_root = Path(args.out)
    model_out = out_root / args.model
    model_out.mkdir(parents=True, exist_ok=True)

    # Score only the cases actually run (so subset runs aren't penalised for
    # un-run cases; for the full 32-case run this is the whole set).
    run_cases = [c for c in cases if c.id in set(all_case_ids)]

    # Load model once
    if not args.render_only:
        if args.model == "minicpm":
            model_obj = _load_model_minicpm()
        else:
            model_obj = _load_model_gpt()

    # Score baselines once
    baselines_path = out_root / "baselines.json"
    if not baselines_path.exists():
        baselines = {
            "oracle": score_run(cases, oracle_run(cases)),
            "always_silent": score_run(cases, always_silent_run(cases)),
            "always_deliver": score_run(cases, always_deliver_run(cases)),
        }
        baselines_path.write_text(json.dumps(baselines, indent=2))

    # Per-condition loop
    for condition in arms:
        cond_dir = model_out / condition
        cond_dir.mkdir(parents=True, exist_ok=True)

        if args.render_only:
            case_runs = _load_existing_emissions(cond_dir, all_case_ids, args.k)
        elif args.model == "minicpm":
            case_runs = _run_condition_minicpm(condition, all_case_ids, args.k, model_obj)
        else:
            case_runs = _run_condition_gpt(condition, all_case_ids, args.k, args.realtime)

        # Persist each run and aggregate
        run_emission_dicts: list[dict] = []  # list of k run dicts for aggregate_runs
        for r_idx in range(args.k):
            run_dict: dict[str, list] = {}
            for cid in all_case_ids:
                emissions = case_runs[cid][r_idx]
                run_dict[cid] = emissions
                # per-case score (single-case run)
                per_case = score_run([c for c in cases if c.id == cid], {cid: emissions})
                _persist_run(cond_dir, cid, r_idx + 1, emissions, per_case)
            run_score = score_run(run_cases, run_dict)
            _persist_scores(cond_dir, r_idx + 1, run_score)
            run_emission_dicts.append(run_dict)

        agg = aggregate_runs(run_cases, run_emission_dicts)
        (cond_dir / "aggregate.json").write_text(json.dumps(agg, indent=2))

    # Write meta.json
    detector_config = {
        "t_lex": delivery_detector.T_LEX,
        "t_emb": delivery_detector.T_EMB,
        "emb_model_id": delivery_detector._EMB_MODEL_ID,
        "emb_model_revision": delivery_detector._EMB_MODEL_REVISION,
        "lexical_only": delivery_detector._LEXICAL_ONLY,
    }
    meta = {
        "model": args.model,
        "conditions": arms,
        "k": args.k,
        "cases": all_case_ids,
        "preregistered_config": preregistered_config(),
        "detector_config": detector_config,
        "tick_tolerance": preregistered_config()["tick_tolerance"],
        "arm_prompts": {k: v for k, v in _ARMS.items() if k in {"vanilla", "prompted", "monitor_stream"}},
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "realtime": args.realtime,
    }
    (model_out / "meta.json").write_text(json.dumps(meta, indent=2))

    print(f"Done. Results in {model_out}/")


if __name__ == "__main__":
    main()
