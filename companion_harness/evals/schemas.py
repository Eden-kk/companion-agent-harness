"""Eval-only schema types (eval-subsystem-spec.md Anchor 3).

These types are referenced only by eval adapters and reporters.
Types referenced by the runtime live in companion_harness.schemas.
Promote to companion_harness.schemas when the orchestrator references them
(CLAUDE.md rule 2 — no abstractions for single-use code).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class MetricValue:
    """Generic scalar / histogram / distribution result for one metric.

    ``value`` accepts ``float | int | dict``; bool is an int subclass in
    Python so it sneaks through via int — callers should avoid passing bool.
    """

    name: str
    value: float | int | dict
    unit: str | None
    aggregation: Literal["scalar", "histogram", "distribution"]


@dataclass(frozen=True)
class BenchmarkResult:
    """Per-case pass/fail + collected metrics."""

    case_id: str
    pass_: bool  # 'pass' is a reserved keyword
    metrics: tuple[MetricValue, ...]
    final_status: str


@dataclass(frozen=True)
class FailureSlice:
    """Causal subgraph fragment explaining a case failure."""

    case_id: str
    causal_event_ids: tuple[str, ...]
    suspected_adapter: str | None
    relevant_policy_inputs: dict = field(default_factory=dict)
    suggested_fix: str | None = None


@dataclass(frozen=True)
class BenchmarkSuiteManifest:
    """Suite-level identity block."""

    name: str
    version: str
    case_count: int


@dataclass(frozen=True)
class ReporterConfig:
    """Output configuration passed to Reporter.render()."""

    output_dir: str
    formats: tuple[str, ...]
