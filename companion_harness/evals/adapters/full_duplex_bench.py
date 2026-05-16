"""Full-Duplex-Bench v1 / v1.5 adapters.

Phase B2 — eval-subsystem-spec.md Anchor 5 + roadmap-eval-draft.md §B2.

Real FDB data requires the Phase C live-examiner pipeline; until then this
module runs in **synthetic mode**: 50 mock cases per variant generated from
a fixed seed.  Synthetic mode is the default and is documented as such.

Synthetic-mode lean: all cases are deterministic, fixture-free stubs that
exercise the eval machinery (CaseSource protocol, BenchmarkAdapter wiring,
FailureSliceExtractor contract) without external data access.  Real FDB
ingestion goes here when Phase C gating prerequisites are met.
"""

from __future__ import annotations

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
# Synthetic case generation
# ---------------------------------------------------------------------------

_SCENARIOS_V1 = [
    "turn_gap_short",
    "turn_gap_long",
    "overlap_barge_in",
    "backchannel_pause",
    "response_delay_high",
    "silence_after_question",
    "filler_overlap",
    "double_speak",
    "abrupt_end",
    "repair_sequence",
]

_SCENARIOS_V1_5 = _SCENARIOS_V1 + [
    "multi_party_floor_grab",
    "noisy_channel_recovery",
    "backpressure_drop",
    "latency_spike",
    "rapid_turn_succession",
]


def _make_synthetic_cases(
    version: str,
    scenarios: list[str],
    count: int,
) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for i in range(count):
        scenario = scenarios[i % len(scenarios)]
        case_id = f"fdb-{version}-{i:04d}-{scenario}"
        cases.append(
            EvaluationCase(
                case_id=case_id,
                stage=2,
                scenario=scenario,
                modalities=["audio"],
                fixture_ref=f"synthetic/{version}/{case_id}.json",
                expected_events=["policy_decision"],
                expected_metrics={},
                consent_class="safe_eval_fixture",
                benchmark_name="full_duplex_bench",
                benchmark_version=version,
                inputs={"synthetic": True, "index": i},
                expected_behavior={"action_class": "silence"},
                fixtures=[],
            )
        )
    return cases


# ---------------------------------------------------------------------------
# CaseSource implementations
# ---------------------------------------------------------------------------

@dataclass
class FullDuplexBenchV1CaseSource:
    """Yields 50 synthetic FDB v1 cases.

    Synthetic mode: no external data access.  Replace ``_SYNTHETIC_CASES``
    assignment with a real FDB fetch when Phase C gating completes.
    """

    name: str = "full_duplex_bench"
    version: str = "v1"
    _count: int = 50

    def __post_init__(self) -> None:
        self._cases = _make_synthetic_cases(self.version, _SCENARIOS_V1, self._count)

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]:
        return iter(self._cases)


assert isinstance(FullDuplexBenchV1CaseSource(), CaseSource)


@dataclass
class FullDuplexBenchV15CaseSource:
    """Yields 50 synthetic FDB v1.5 cases (extended scenario set).

    Synthetic mode: same machinery as V1; adds 5 more scenario labels.
    """

    name: str = "full_duplex_bench"
    version: str = "v1.5"
    _count: int = 50

    def __post_init__(self) -> None:
        self._cases = _make_synthetic_cases(self.version, _SCENARIOS_V1_5, self._count)

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]:
        return iter(self._cases)


assert isinstance(FullDuplexBenchV15CaseSource(), CaseSource)


# ---------------------------------------------------------------------------
# Scenario driver (synthetic — no subprocess, no harness spin-up)
# ---------------------------------------------------------------------------

class _SyntheticFDBDriver:
    """Returns a stub ReplayRun for each synthetic FDB case.

    In synthetic mode every case completes with final_status="completed" and
    an empty event log (no live harness is involved).  The FailureSliceExtractor
    operates on the stub ReplayRun + a pre-built event graph.
    """

    async def run(self, case: EvaluationCase, harness_factory: object, run_config: object) -> ReplayRun:
        import time
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return ReplayRun(
            run_id=f"fdb-synthetic-{case.case_id}",
            case_id=case.case_id,
            implementation_config_version="full_duplex_bench-synthetic",
            policy_version="full_duplex_bench-synthetic",
            started_at=now,
            finished_at=now,
            results={"synthetic": True},
            failures=[],
            event_log_path=None,
            timing_mode="synthetic_clock",
            final_status="completed",
        )


# ---------------------------------------------------------------------------
# No-op slicer — defers to CausalFailureSliceExtractor wired externally
# ---------------------------------------------------------------------------

class _NoOpFDBSlicer:
    def extract(
        self,
        case: EvaluationCase,
        replay_run: ReplayRun,
        result: BenchmarkResult,
    ) -> list[FailureSlice]:
        return []


assert isinstance(_NoOpFDBSlicer(), FailureSliceExtractor)


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------

def build_v1() -> BenchmarkAdapter:
    return BenchmarkAdapter(
        name="full_duplex_bench",
        version="v1",
        case_source=FullDuplexBenchV1CaseSource(),
        scenario_driver=_SyntheticFDBDriver(),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpFDBSlicer(),
        reporters=[],
    )


def build_v1_5() -> BenchmarkAdapter:
    return BenchmarkAdapter(
        name="full_duplex_bench",
        version="v1.5",
        case_source=FullDuplexBenchV15CaseSource(),
        scenario_driver=_SyntheticFDBDriver(),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpFDBSlicer(),
        reporters=[],
    )
