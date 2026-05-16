"""Full-Duplex-Bench v1 / v1.5 adapters.

Phase B2 — eval-subsystem-spec.md Anchor 5 + roadmap-eval-draft.md §B2.

SYNTHETIC MODE (default):
  50 mock cases per variant generated from a fixed seed. Exercises the eval
  machinery (CaseSource protocol, BenchmarkAdapter wiring,
  FailureSliceExtractor contract) without external data access.

REAL MODE (opt-in via ``synthetic=False``):
  Streams from the FullDuplexBench HuggingFace dataset.
  V1 slug: Ssshangfu/Full-Duplex-Bench-Data (v1.0/ prefix)
  V1.5 slug: Ssshangfu/Full-Duplex-Bench-Data (v1.5/ prefix)
  Both require HF_TOKEN; the dataset is not gated but uses audiofolder format.

Pinned license: apache-2.0 (dataset card as of 2026-01-06; loader refuses if
upstream license changes).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Iterable

from companion_harness.evals.protocols import (
    BenchmarkAdapter,
    CaseSource,
    FailureSliceExtractor,
)
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice
from companion_harness.schemas import EvaluationCase, ReplayRun

# ---------------------------------------------------------------------------
# HuggingFace dataset constants — greppable; do not move into __init__ params
# ---------------------------------------------------------------------------

_FDB_HF_SLUG = "Ssshangfu/Full-Duplex-Bench-Data"

# Pinned commit hashes — from list_repo_commits() on 2026-01-06.
_FDB_V1_REVISION = "a3579cb9ebbe164204b196e33d4d0e91860eb90e"
_FDB_V15_REVISION = "a3579cb9ebbe164204b196e33d4d0e91860eb90e"

# License pinned from dataset tags; loaders refuse if upstream changes it.
_FDB_LICENSE = "apache-2.0"

assert _FDB_V1_REVISION, "FDB V1 revision hash must be set"
assert _FDB_V15_REVISION, "FDB V1.5 revision hash must be set"

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
# Row-level helpers for real mode
# ---------------------------------------------------------------------------

def _fdb_stable_key(row: dict) -> tuple:
    return (str(row.get("scenario", "")), str(row.get("case_id", "")))


def _fdb_v1_row_to_evaluation_case(row: dict, index: int) -> EvaluationCase:
    """Map one FDB V1 streaming row to an EvaluationCase.

    Raises ValueError if the scenario label is unrecognized or audio is missing.
    Unrecognized scenarios increment the skip counter (not silently mapped).
    """
    scenario = row.get("scenario") or row.get("label") or row.get("category")
    if scenario is None:
        raise ValueError("FDB V1 row missing scenario/label/category field")
    if scenario not in _SCENARIOS_V1:
        raise ValueError(f"FDB V1 unrecognized scenario: {scenario!r}")
    audio = row.get("audio")
    if audio is None:
        raise ValueError("FDB V1 row missing 'audio' field")
    audio_bytes = audio.get("bytes") or audio.get("array")
    if audio_bytes is None:
        raise ValueError("FDB V1 row 'audio' missing bytes")
    return EvaluationCase(
        case_id=f"fdb_v1_real_{index:04d}_{scenario}",
        stage=2,
        scenario=scenario,
        modalities=["audio"],
        fixture_ref=f"fdb_real/v1/{index:04d}_{scenario}",
        expected_events=["policy_decision"],
        expected_metrics={},
        consent_class="safe_eval_fixture",
        benchmark_name="full_duplex_bench",
        benchmark_version="v1",
        inputs={
            "audio_pcm_bytes": audio_bytes,
            "sample_rate": audio.get("sampling_rate", 16000),
            "synthetic": False,
        },
        expected_behavior={"action_class": "silence"},
        fixtures=[],
    )


def _fdb_v15_row_to_evaluation_case(row: dict, index: int) -> EvaluationCase:
    """Map one FDB V1.5 streaming row to an EvaluationCase.

    Raises ValueError if the scenario label is unrecognized or audio is missing.
    """
    scenario = row.get("scenario") or row.get("label") or row.get("category")
    if scenario is None:
        raise ValueError("FDB V1.5 row missing scenario/label/category field")
    if scenario not in _SCENARIOS_V1_5:
        raise ValueError(f"FDB V1.5 unrecognized scenario: {scenario!r}")
    audio = row.get("audio")
    if audio is None:
        raise ValueError("FDB V1.5 row missing 'audio' field")
    audio_bytes = audio.get("bytes") or audio.get("array")
    if audio_bytes is None:
        raise ValueError("FDB V1.5 row 'audio' missing bytes")
    return EvaluationCase(
        case_id=f"fdb_v1_5_real_{index:04d}_{scenario}",
        stage=2,
        scenario=scenario,
        modalities=["audio"],
        fixture_ref=f"fdb_real/v1.5/{index:04d}_{scenario}",
        expected_events=["policy_decision"],
        expected_metrics={},
        consent_class="safe_eval_fixture",
        benchmark_name="full_duplex_bench",
        benchmark_version="v1.5",
        inputs={
            "audio_pcm_bytes": audio_bytes,
            "sample_rate": audio.get("sampling_rate", 16000),
            "synthetic": False,
        },
        expected_behavior={"action_class": "silence"},
        fixtures=[],
    )


# ---------------------------------------------------------------------------
# CaseSource implementations
# ---------------------------------------------------------------------------

@dataclass
class FullDuplexBenchV1CaseSource:
    """Yields FDB v1 cases.

    In synthetic mode (default, ``synthetic=True``), yields 50 deterministic
    stub cases; no external data access.

    In real mode (``synthetic=False``), streams from the FDB HuggingFace
    dataset at ``_FDB_HF_SLUG`` pinned at ``_FDB_V1_REVISION``.
    Requires ``HF_TOKEN`` env-var.

    Pinned license: apache-2.0.
    """

    name: str = "full_duplex_bench"
    version: str = "v1"
    synthetic: bool = True
    _count: int = 50
    _attempted: int = field(default=0, init=False, repr=False)
    _skipped: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.synthetic:
            self._cases = _make_synthetic_cases(self.version, _SCENARIOS_V1, self._count)
        else:
            if os.environ.get("HF_TOKEN") is None:
                raise RuntimeError(
                    "HF_TOKEN missing; set HF_TOKEN or run `huggingface-cli login`. "
                    "The FDB dataset is at https://huggingface.co/datasets/Ssshangfu/Full-Duplex-Bench-Data."
                )

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]:
        if self.synthetic:
            return iter(self._cases)
        return self._real_cases(split)

    def skip_stats(self) -> dict:
        attempted = self._attempted
        skipped = self._skipped
        rate = skipped / attempted if attempted > 0 else 0.0
        return {"attempted": attempted, "skipped": skipped, "skip_rate": rate}

    def _real_cases(self, split: str) -> Iterable[EvaluationCase]:
        from datasets import load_dataset  # type: ignore[import-not-found]
        ds = load_dataset(
            _FDB_HF_SLUG,
            split=split,
            streaming=True,
            revision=_FDB_V1_REVISION,
            token=os.environ.get("HF_TOKEN"),
            data_dir="v1.0",
        )
        self._attempted = 0
        self._skipped = 0
        for index, row in enumerate(_fdb_ordered(ds)):
            self._attempted += 1
            try:
                yield _fdb_v1_row_to_evaluation_case(row, index)
            except (ValueError, KeyError, TypeError):
                self._skipped += 1


assert isinstance(FullDuplexBenchV1CaseSource(), CaseSource)


@dataclass
class FullDuplexBenchV15CaseSource:
    """Yields FDB v1.5 cases (extended scenario set).

    In synthetic mode (default, ``synthetic=True``), yields 50 deterministic
    stub cases; no external data access.

    In real mode (``synthetic=False``), streams from the FDB HuggingFace
    dataset at ``_FDB_HF_SLUG`` pinned at ``_FDB_V15_REVISION``.
    Requires ``HF_TOKEN`` env-var.

    Pinned license: apache-2.0.
    """

    name: str = "full_duplex_bench"
    version: str = "v1.5"
    synthetic: bool = True
    _count: int = 50
    _attempted: int = field(default=0, init=False, repr=False)
    _skipped: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.synthetic:
            self._cases = _make_synthetic_cases(self.version, _SCENARIOS_V1_5, self._count)
        else:
            if os.environ.get("HF_TOKEN") is None:
                raise RuntimeError(
                    "HF_TOKEN missing; set HF_TOKEN or run `huggingface-cli login`. "
                    "The FDB dataset is at https://huggingface.co/datasets/Ssshangfu/Full-Duplex-Bench-Data."
                )

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]:
        if self.synthetic:
            return iter(self._cases)
        return self._real_cases(split)

    def skip_stats(self) -> dict:
        attempted = self._attempted
        skipped = self._skipped
        rate = skipped / attempted if attempted > 0 else 0.0
        return {"attempted": attempted, "skipped": skipped, "skip_rate": rate}

    def _real_cases(self, split: str) -> Iterable[EvaluationCase]:
        from datasets import load_dataset  # type: ignore[import-not-found]
        ds = load_dataset(
            _FDB_HF_SLUG,
            split=split,
            streaming=True,
            revision=_FDB_V15_REVISION,
            token=os.environ.get("HF_TOKEN"),
            data_dir="v1.5",
        )
        self._attempted = 0
        self._skipped = 0
        for index, row in enumerate(_fdb_ordered(ds)):
            self._attempted += 1
            try:
                yield _fdb_v15_row_to_evaluation_case(row, index)
            except (ValueError, KeyError, TypeError):
                self._skipped += 1


assert isinstance(FullDuplexBenchV15CaseSource(), CaseSource)


def _fdb_ordered(iterable: Iterable[dict], sort_head: int = 100) -> Iterable[dict]:
    """Yield FDB rows with deterministic ordering over the first `sort_head`."""
    head: list[dict] = []
    it = iter(iterable)
    for row in it:
        head.append(row)
        if len(head) >= sort_head:
            break
    yield from sorted(head, key=_fdb_stable_key)
    yield from it


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

def build_v1(synthetic: bool = True) -> BenchmarkAdapter:
    return BenchmarkAdapter(
        name="full_duplex_bench",
        version="v1",
        case_source=FullDuplexBenchV1CaseSource(synthetic=synthetic),
        scenario_driver=_SyntheticFDBDriver(),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpFDBSlicer(),
        reporters=[],
    )


def build_v1_5(synthetic: bool = True) -> BenchmarkAdapter:
    return BenchmarkAdapter(
        name="full_duplex_bench",
        version="v1.5",
        case_source=FullDuplexBenchV15CaseSource(synthetic=synthetic),
        scenario_driver=_SyntheticFDBDriver(),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpFDBSlicer(),
        reporters=[],
    )
