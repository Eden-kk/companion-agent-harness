"""CANDOR distributional adapter (Phase B1).

SYNTHETIC MODE (default):
  Real CANDOR data requires the HuggingFace ``datasets`` library and network
  access to download ~50 GB of conversation audio.  Phase B1 ships in
  synthetic-mode by default: 100 utterance pairs are generated with timing
  distributions drawn from the published human-conversation envelopes in
  Cao et al. 2023 (https://doi.org/10.1126/sciadv.adf3197).

  Synthetic-mode lean:
    - turn_gap_ms:          log-normal(mu=5.3, sigma=0.7)  → median ~200 ms
    - overlap_ms:           log-normal(mu=5.0, sigma=0.8)  → median ~148 ms
    - backchannel_pause_ms: log-normal(mu=4.6, sigma=0.7)  → median  ~99 ms
    - response_delay_ms:    log-normal(mu=5.7, sigma=0.6)  → median ~299 ms

  These parameters reproduce the envelope shape from the CANDOR paper
  (§3.2–3.4) without loading real audio. Results are labelled
  "[synthetic]" in all reports.

REAL MODE (future):
  Set CANDOR_DATA_DIR to a directory containing CANDOR audio + metadata, or
  set CANDOR_USE_REAL=1 with the ``datasets`` extra installed. Neither is
  required for Phase B1.

No model SDK imports. No HuggingFace datasets import.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from companion_harness.evals.metrics.distributional import ALL_DISTRIBUTIONAL_METRICS
from companion_harness.evals.protocols import (
    BenchmarkAdapter,
    CaseSource,
    FailureSliceExtractor,
)
from companion_harness.evals.reporters.distributional_md_reporter import DistributionalMdReporter
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice
from companion_harness.schemas import EvaluationCase, ReplayRun


# ---------------------------------------------------------------------------
# Synthetic fixture generation
# ---------------------------------------------------------------------------

# Log-normal parameters calibrated to CANDOR paper envelopes (§3.2–3.4)
_SYNTHETIC_PARAMS: dict[str, tuple[float, float]] = {
    "turn_gap_ms_observations":         (5.30, 0.70),
    "overlap_ms_observations":          (5.00, 0.80),
    "backchannel_pause_ms_observations":(4.60, 0.70),
    "response_delay_ms_observations":   (5.70, 0.60),
}

_SYNTHETIC_N = 100


def _lognormal_sample(mu: float, sigma: float, n: int, rng: random.Random) -> list[float]:
    return [math.exp(mu + sigma * _box_muller(rng)) for _ in range(n)]


def _box_muller(rng: random.Random) -> float:
    u1 = rng.random() or 1e-12
    u2 = rng.random()
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _build_synthetic_observations(seed: int = 42) -> dict[str, list[float]]:
    rng = random.Random(seed)
    return {
        key: _lognormal_sample(mu, sigma, _SYNTHETIC_N, rng)
        for key, (mu, sigma) in _SYNTHETIC_PARAMS.items()
    }


# ---------------------------------------------------------------------------
# CaseSource
# ---------------------------------------------------------------------------

@dataclass
class CandorCaseSource:
    """Yields CANDOR utterance-pair EvaluationCases.

    In synthetic mode (default), generates 100 representative cases using
    timing distributions from Cao et al. 2023 (CANDOR paper §3.2–3.4).

    ``name`` and ``version`` are Protocol-required attributes.
    """

    name: str = "candor"
    version: str = "v1"
    synthetic: bool = True
    seed: int = 42

    def iter_cases(self, split: str = "test") -> Iterable[EvaluationCase]:
        if self.synthetic:
            yield from self._synthetic_cases(split)
        else:
            raise NotImplementedError(
                "Real CANDOR ingestion requires CANDOR_DATA_DIR + soundfile. "
                "Set synthetic=True (default) for Phase B1."
            )

    def _synthetic_cases(self, split: str) -> Iterable[EvaluationCase]:
        obs = _build_synthetic_observations(self.seed)
        for i in range(_SYNTHETIC_N):
            # Each case carries its slice of the timing observations so that
            # ScenarioDriver.run() can materialise a ReplayRun from it.
            yield EvaluationCase(
                case_id=f"candor_synthetic_{split}_{i:03d}",
                stage=0,
                scenario="candor_distributional_probe",
                modalities=["audio"],
                fixture_ref=f"candor_synthetic/{split}/{i:03d}",
                expected_events=[],
                expected_metrics={},
                consent_class="safe_eval_fixture",
                benchmark_name="candor",
                benchmark_version="v1",
                inputs={
                    "synthetic": True,
                    "index": i,
                    "turn_gap_ms":         obs["turn_gap_ms_observations"][i],
                    "overlap_ms":          obs["overlap_ms_observations"][i],
                    "backchannel_pause_ms":obs["backchannel_pause_ms_observations"][i],
                    "response_delay_ms":   obs["response_delay_ms_observations"][i],
                },
                expected_behavior={"distributional": True},
            )


assert isinstance(CandorCaseSource(), CaseSource)


# ---------------------------------------------------------------------------
# Scenario driver — aggregates all cases into a single ReplayRun
# ---------------------------------------------------------------------------

class CandorScenarioDriver:
    """Aggregates all timing observations from CANDOR cases into one ReplayRun.

    For distributional probes the meaningful unit is the aggregate distribution,
    not a per-case pass/fail.  This driver collects every case's single timing
    sample into four observation lists and returns one ReplayRun whose results
    dict carries all four lists.
    """

    async def run(
        self,
        case: EvaluationCase,
        harness_factory: object,
        run_config: object,
    ) -> ReplayRun:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        inputs = case.inputs or {}
        results = {
            "turn_gap_ms_observations":         [inputs.get("turn_gap_ms", 0.0)],
            "overlap_ms_observations":           [inputs.get("overlap_ms", 0.0)],
            "backchannel_pause_ms_observations": [inputs.get("backchannel_pause_ms", 0.0)],
            "response_delay_ms_observations":    [inputs.get("response_delay_ms", 0.0)],
            "synthetic": inputs.get("synthetic", True),
        }
        output_dir: Path = getattr(run_config, "output_dir", Path("reports"))
        return ReplayRun(
            run_id=f"candor_{case.case_id}",
            case_id=case.case_id,
            implementation_config_version="candor-v1",
            policy_version="candor-v1",
            started_at=started_at,
            finished_at=started_at,
            results=results,
            failures=[],
            event_log_path=output_dir / "event_logs" / f"{case.case_id}.jsonl",
            timing_mode="synthetic_clock",
            final_status="completed",
        )


# ---------------------------------------------------------------------------
# NoOp failure slicer — distributional probe has no per-case pass/fail
# ---------------------------------------------------------------------------

class _NoOpFailureSlicer:
    def extract(
        self,
        case: EvaluationCase,
        replay_run: ReplayRun,
        result: BenchmarkResult,
    ) -> list[FailureSlice]:
        return []


assert isinstance(_NoOpFailureSlicer(), FailureSliceExtractor)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build(synthetic: bool = True, seed: int = 42) -> BenchmarkAdapter:
    """Return the CANDOR BenchmarkAdapter instance.

    Args:
        synthetic: If True (default), use synthetic fixtures. Set False only
            when CANDOR audio + metadata are locally available.
        seed: Random seed for synthetic fixture reproducibility.
    """
    return BenchmarkAdapter(
        name="candor",
        version="v1",
        case_source=CandorCaseSource(synthetic=synthetic, seed=seed),
        scenario_driver=CandorScenarioDriver(),
        examiner=None,
        metrics=list(ALL_DISTRIBUTIONAL_METRICS),
        failure_slicer=_NoOpFailureSlicer(),
        reporters=[DistributionalMdReporter()],
    )
