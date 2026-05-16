"""VocalBench adapter (Eval Phase D).

SYNTHETIC MODE (default):
  Real VocalBench data requires the original dataset files and network access.
  This adapter ships in synthetic-mode by default: 50 vocal-quality assessment
  cases are generated from a fixed seed, exercising the eval machinery without
  external data access.  Results are labelled "[synthetic]" in all reports.

REAL MODE (future):
  Set ``synthetic=False`` to reach the real-data path.  Real mode raises
  ``NotImplementedError`` until VocalBench ingestion is implemented post-Phase-C.

No model SDK imports.  No external data dependencies in synthetic mode.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable

from companion_harness.evals.protocols import (
    BenchmarkAdapter,
    CaseSource,
    FailureSliceExtractor,
)
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice
from companion_harness.schemas import EvaluationCase, ReplayRun

# ---------------------------------------------------------------------------
# Scenario taxonomy
# ---------------------------------------------------------------------------

_VOCAL_SCENARIOS = [
    "naturalness_high",
    "naturalness_low",
    "prosody_appropriate",
    "prosody_flat",
    "prosody_exaggerated",
    "clarity_high",
    "clarity_low_noise",
    "clarity_mumbled",
    "pace_normal",
    "pace_too_fast",
    "pace_too_slow",
    "pitch_neutral",
    "pitch_monotone",
    "pitch_varied",
    "emotional_warmth",
    "emotional_neutral",
    "emotional_detachment",
    "filler_words_few",
    "filler_words_many",
    "breath_artifacts",
    "disfluency_light",
    "disfluency_heavy",
    "volume_appropriate",
    "volume_too_quiet",
    "volume_too_loud",
]

_SYNTHETIC_COUNT = 50


def _make_synthetic_cases(seed: int) -> list[EvaluationCase]:
    import random
    rng = random.Random(seed)
    cases: list[EvaluationCase] = []
    for i in range(_SYNTHETIC_COUNT):
        scenario = _VOCAL_SCENARIOS[i % len(_VOCAL_SCENARIOS)]
        case_id = f"vocalbench-synthetic-{i:04d}-{scenario}"
        # Synthetic quality score: float in [1.0, 5.0], seeded for determinism
        quality_score = round(1.0 + rng.random() * 4.0, 2)
        cases.append(
            EvaluationCase(
                case_id=case_id,
                stage=4,
                scenario=scenario,
                modalities=["audio"],
                fixture_ref=f"synthetic/vocalbench/{case_id}.json",
                expected_events=["policy_decision"],
                expected_metrics={"quality_score": quality_score},
                consent_class="safe_eval_fixture",
                benchmark_name="vocalbench",
                benchmark_version="v1",
                inputs={"synthetic": True, "index": i, "quality_score": quality_score},
                expected_behavior={"vocal_quality": scenario},
                fixtures=[],
            )
        )
    return cases


# ---------------------------------------------------------------------------
# CaseSource
# ---------------------------------------------------------------------------

@dataclass
class VocalBenchCaseSource:
    """Yields ~50 synthetic vocal-quality assessment cases.

    Synthetic mode: no external data access.  Replace ``_make_synthetic_cases``
    with a real VocalBench fetch when Phase C gating completes and the dataset
    is locally available.
    """

    name: str = "vocalbench"
    version: str = "v1"
    synthetic: bool = True
    seed: int = 42

    def __post_init__(self) -> None:
        if self.synthetic:
            self._cases = _make_synthetic_cases(self.seed)
        else:
            self._cases = None

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]:
        if not self.synthetic:
            raise NotImplementedError(
                "Real VocalBench ingestion is not yet implemented. "
                "Use synthetic=True (default) for Phase D."
            )
        return iter(self._cases)  # type: ignore[arg-type]


assert isinstance(VocalBenchCaseSource(), CaseSource)


# ---------------------------------------------------------------------------
# Scenario driver — stub ReplayRun per case
# ---------------------------------------------------------------------------

class _SyntheticVocalBenchDriver:
    """Returns a stub ReplayRun for each synthetic VocalBench case.

    Synthetic mode: no live harness spin-up.  Each case produces a
    completed ReplayRun whose results dict carries the case quality_score.
    """

    async def run(
        self,
        case: EvaluationCase,
        harness_factory: object,
        run_config: object,
    ) -> ReplayRun:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        inputs = case.inputs or {}
        return ReplayRun(
            run_id=f"vocalbench-synthetic-{case.case_id}",
            case_id=case.case_id,
            implementation_config_version="vocalbench-synthetic",
            policy_version="vocalbench-synthetic",
            started_at=now,
            finished_at=now,
            results={
                "synthetic": True,
                "quality_score": inputs.get("quality_score", 0.0),
                "scenario": case.scenario,
            },
            failures=[],
            event_log_path=None,
            timing_mode="synthetic_clock",
            final_status="completed",
        )


# ---------------------------------------------------------------------------
# No-op failure slicer
# ---------------------------------------------------------------------------

class _NoOpVocalBenchSlicer:
    def extract(
        self,
        case: EvaluationCase,
        replay_run: ReplayRun,
        result: BenchmarkResult,
    ) -> list[FailureSlice]:
        return []


assert isinstance(_NoOpVocalBenchSlicer(), FailureSliceExtractor)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_vocalbench(synthetic: bool = True, seed: int = 42) -> BenchmarkAdapter:
    """Return the VocalBench BenchmarkAdapter instance.

    Args:
        synthetic: If True (default), use synthetic fixtures.  Set False only
            when VocalBench dataset files are locally available (post-Phase-C).
        seed: Random seed for synthetic fixture reproducibility.
    """
    return BenchmarkAdapter(
        name="vocalbench",
        version="v1",
        case_source=VocalBenchCaseSource(synthetic=synthetic, seed=seed),
        scenario_driver=_SyntheticVocalBenchDriver(),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpVocalBenchSlicer(),
        reporters=[],
    )
