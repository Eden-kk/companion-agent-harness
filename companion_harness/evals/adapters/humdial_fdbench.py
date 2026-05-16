"""HumDial-FDBench adapter (Eval Phase D).

SYNTHETIC MODE (default):
  Real HumDial-FDBench data requires the original dataset files and the
  ``datasets`` library.  This adapter ships in synthetic-mode by default:
  ~30 dialog cases are generated from a fixed seed across four categories
  (dialog_act_recognition, humor_response, turn_yielding, interrupt_recovery),
  exercising the eval machinery without external data access.  Results are
  labelled "[synthetic]" in all reports.

REAL MODE (future):
  Set ``synthetic=False`` to reach the real-data path.  Real mode raises
  ``NotImplementedError`` until HumDial-FDBench ingestion is implemented.

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
# Category taxonomy
# ---------------------------------------------------------------------------

_HUMDIAL_CATEGORIES = [
    "dialog_act_recognition",
    "humor_response",
    "turn_yielding",
    "interrupt_recovery",
]

_SYNTHETIC_COUNT = 30


def _make_synthetic_cases(seed: int) -> list[EvaluationCase]:
    import random
    rng = random.Random(seed)
    cases: list[EvaluationCase] = []
    for i in range(_SYNTHETIC_COUNT):
        category = _HUMDIAL_CATEGORIES[i % len(_HUMDIAL_CATEGORIES)]
        case_id = f"humdial-fdbench-synthetic-{i:04d}-{category}"
        # Synthetic score: float in [0.0, 1.0], seeded for determinism
        score = round(rng.random(), 3)
        cases.append(
            EvaluationCase(
                case_id=case_id,
                stage=4,
                scenario=category,
                modalities=["audio"],
                fixture_ref=f"synthetic/humdial_fdbench/{case_id}.json",
                expected_events=["policy_decision"],
                expected_metrics={"score": score},
                consent_class="safe_eval_fixture",
                benchmark_name="humdial_fdbench",
                benchmark_version="v1",
                inputs={"synthetic": True, "index": i, "score": score},
                expected_behavior={"category": category},
                fixtures=[],
            )
        )
    return cases


# ---------------------------------------------------------------------------
# CaseSource
# ---------------------------------------------------------------------------

@dataclass
class HumDialFDBenchCaseSource:
    """Yields ~30 synthetic dialog cases across 4 HumDial-FDBench categories.

    Synthetic mode: no external data access.  Replace ``_make_synthetic_cases``
    with a real HumDial-FDBench fetch when the dataset is locally available.
    """

    name: str = "humdial_fdbench"
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
                "HumDial-FDBench dataset not yet bundled; install datasets "
                "and add a real-mode loader."
            )
        return iter(self._cases)  # type: ignore[arg-type]


assert isinstance(HumDialFDBenchCaseSource(), CaseSource)


# ---------------------------------------------------------------------------
# Scenario driver — stub ReplayRun per case
# ---------------------------------------------------------------------------

class _SyntheticHumDialFDBenchDriver:
    """Returns a stub ReplayRun for each synthetic HumDial-FDBench case."""

    async def run(
        self,
        case: EvaluationCase,
        harness_factory: object,
        run_config: object,
    ) -> ReplayRun:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        inputs = case.inputs or {}
        return ReplayRun(
            run_id=f"humdial-fdbench-synthetic-{case.case_id}",
            case_id=case.case_id,
            implementation_config_version="humdial-fdbench-synthetic",
            policy_version="humdial-fdbench-synthetic",
            started_at=now,
            finished_at=now,
            results={
                "synthetic": True,
                "score": inputs.get("score", 0.0),
                "category": case.scenario,
            },
            failures=[],
            event_log_path=None,
            timing_mode="synthetic_clock",
            final_status="completed",
        )


# ---------------------------------------------------------------------------
# No-op failure slicer
# ---------------------------------------------------------------------------

class _NoOpHumDialFDBenchSlicer:
    def extract(
        self,
        case: EvaluationCase,
        replay_run: ReplayRun,
        result: BenchmarkResult,
    ) -> list[FailureSlice]:
        return []


assert isinstance(_NoOpHumDialFDBenchSlicer(), FailureSliceExtractor)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_humdial_fdbench(synthetic: bool = True, seed: int = 42) -> BenchmarkAdapter:
    """Return the HumDial-FDBench BenchmarkAdapter instance.

    Args:
        synthetic: If True (default), use synthetic fixtures.  Set False only
            when HumDial-FDBench dataset files are locally available.
        seed: Random seed for synthetic fixture reproducibility.
    """
    return BenchmarkAdapter(
        name="humdial_fdbench",
        version="v1",
        case_source=HumDialFDBenchCaseSource(synthetic=synthetic, seed=seed),
        scenario_driver=_SyntheticHumDialFDBenchDriver(),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpHumDialFDBenchSlicer(),
        reporters=[],
    )
