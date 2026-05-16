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


# ---------------------------------------------------------------------------
# Eval event-type schema table (eval-subsystem-spec.md §New event types)
#
# Classification axes for eval-bookkeeping event types introduced in Phase A.5.
# Mirrors the structure of companion_harness/v0_1g_event_schema.py.
# Axis values are stamped onto every emitted Event; retention_policy_ids
# referenced here must exist in companion_harness/replay_privacy_policy.yaml.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EvalEventSchema:
    """Classification axes for one eval-subsystem event type."""

    payload_kind: str
    subject_class: str
    sensitivity: str
    retention_policy_id: str


EVAL_EVENT_TYPE_SCHEMAS: dict[str, EvalEventSchema] = {
    "fixture_audio_chunk_injected": EvalEventSchema(
        payload_kind="raw_audio",
        subject_class="self",
        sensitivity="sensitive",
        retention_policy_id="raw_media_default_300s",
    ),
    "synthetic_clock_tick": EvalEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="eval_run_30d",
    ),
    "benchmark_case_started": EvalEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="eval_run_30d",
    ),
    "benchmark_case_completed": EvalEventSchema(
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="eval_run_30d",
    ),
}
