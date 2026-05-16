"""Six composable eval Protocols + BenchmarkAdapter frozen dataclass.

eval-subsystem-spec.md Anchor 5: six small protocols, not one god-protocol.
Each benchmark adapter populates the protocols it needs; missing ones are None.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Protocol, runtime_checkable

from companion_harness.schemas import Event, EvaluationCase, ReplayRun

if TYPE_CHECKING:
    from companion_harness.evals.schemas import BenchmarkResult, FailureSlice, MetricValue


@runtime_checkable
class CaseSource(Protocol):
    name: str
    version: str

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]: ...


@runtime_checkable
class ScenarioDriver(Protocol):
    async def run(self, case: EvaluationCase, harness_factory: object, run_config: object) -> ReplayRun: ...


@runtime_checkable
class Examiner(Protocol):
    async def respond(self, event: Event) -> object: ...


@runtime_checkable
class Metric(Protocol):
    name: str

    def compute(self, replay_run: ReplayRun) -> "MetricValue": ...


@runtime_checkable
class FailureSliceExtractor(Protocol):
    def extract(self, case: EvaluationCase, replay_run: ReplayRun, result: "BenchmarkResult") -> list["FailureSlice"]: ...


@runtime_checkable
class Reporter(Protocol):
    def render(self, results: list["BenchmarkResult"], output_dir: Path) -> None: ...


@dataclass(frozen=True)
class BenchmarkAdapter:
    """Composes the six protocols into one runnable benchmark adapter.

    eval-subsystem-spec.md Anchor 5 — 7 fields.
    Missing protocols (examiner for static benchmarks, case_source for
    harness-native) are None.
    """

    name: str
    version: str
    case_source: CaseSource | None
    scenario_driver: ScenarioDriver
    examiner: Examiner | None
    metrics: list[Metric]
    failure_slicer: FailureSliceExtractor
    reporters: list[Reporter]
