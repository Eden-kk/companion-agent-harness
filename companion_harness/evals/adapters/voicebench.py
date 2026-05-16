"""VoiceBench adapter (Phase D).

VoiceBench (Su et al. 2024) evaluates voice-instruction-following across
categories: open-ended QA, factual recall, safety refusal, instruction
length, and robustness to ASR noise.

SYNTHETIC MODE (default):
  Real VoiceBench data requires the HuggingFace ``datasets`` library and
  audio preprocessing to replay through the harness.  Phase D ships in
  synthetic-mode by default: ~50 fixture cases are generated deterministically,
  covering the benchmark's five published category distributions.

  Synthetic-mode lean:
    - openqa:    12 cases — open-ended conversational questions
    - factual:   12 cases — factual recall with expected answer present
    - safety:     8 cases — refusal or redirection expected
    - instruct:  10 cases — multi-step instruction adherence
    - asr_noise:  8 cases — simulated ASR error injection patterns

  Cases are labelled "[synthetic]" in all reports.

REAL MODE (not implemented):
  Raises ``NotImplementedError`` with an explicit message about the
  ``datasets`` dependency.  Real ingestion goes here when the HuggingFace
  VoiceBench loader is available in the [eval] extra.

No model SDK imports.  No HuggingFace datasets import.
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
# Synthetic case generation
# ---------------------------------------------------------------------------

_CATEGORY_COUNTS: dict[str, int] = {
    "openqa":    12,
    "factual":   12,
    "safety":     8,
    "instruct":  10,
    "asr_noise":  8,
}

_CATEGORY_EXPECTED: dict[str, dict] = {
    "openqa":    {"action_class": "full_response"},
    "factual":   {"action_class": "full_response", "factual_grounded": True},
    "safety":    {"action_class": "silence_or_refusal"},
    "instruct":  {"action_class": "full_response", "steps_followed": True},
    "asr_noise": {"action_class": "full_response", "noise_robust": True},
}


def _make_synthetic_cases() -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    for category, count in _CATEGORY_COUNTS.items():
        expected = _CATEGORY_EXPECTED[category]
        for i in range(count):
            case_id = f"voicebench-{category}-{i:03d}"
            cases.append(
                EvaluationCase(
                    case_id=case_id,
                    stage=0,
                    scenario=f"voicebench_{category}",
                    modalities=["audio"],
                    fixture_ref=f"synthetic/voicebench/{category}/{i:03d}.json",
                    expected_events=["policy_decision"],
                    expected_metrics={},
                    consent_class="safe_eval_fixture",
                    benchmark_name="voicebench",
                    benchmark_version="v1",
                    inputs={"synthetic": True, "category": category, "index": i},
                    expected_behavior=expected,
                    fixtures=[],
                )
            )
    return cases


_SYNTHETIC_CASES: list[EvaluationCase] = _make_synthetic_cases()


# ---------------------------------------------------------------------------
# CaseSource
# ---------------------------------------------------------------------------

@dataclass
class VoiceBenchCaseSource:
    """Yields VoiceBench EvaluationCases.

    In synthetic mode (default), generates ~50 cases covering VoiceBench's
    five published category distributions without external data access.

    ``name`` and ``version`` are Protocol-required attributes.
    """

    name: str = "voicebench"
    version: str = "v1"
    synthetic: bool = True

    def iter_cases(self, split: str = "test") -> Iterable[EvaluationCase]:
        if not self.synthetic:
            raise NotImplementedError(
                "Real VoiceBench ingestion requires the HuggingFace ``datasets`` "
                "library and audio preprocessing (not in [eval] extra yet). "
                "Set synthetic=True (default) for Phase D."
            )
        return iter(_SYNTHETIC_CASES)


assert isinstance(VoiceBenchCaseSource(), CaseSource)


# ---------------------------------------------------------------------------
# Scenario driver (synthetic — stub ReplayRun per case)
# ---------------------------------------------------------------------------

class _SyntheticVoiceBenchDriver:
    """Returns a stub ReplayRun for each synthetic VoiceBench case.

    Synthetic mode: no live harness spin-up; exercises eval machinery only.
    """

    async def run(
        self,
        case: EvaluationCase,
        harness_factory: object,
        run_config: object,
    ) -> ReplayRun:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return ReplayRun(
            run_id=f"voicebench-synthetic-{case.case_id}",
            case_id=case.case_id,
            implementation_config_version="voicebench-synthetic",
            policy_version="voicebench-synthetic",
            started_at=now,
            finished_at=now,
            results={"synthetic": True, "category": (case.inputs or {}).get("category")},
            failures=[],
            event_log_path=None,
            timing_mode="synthetic_clock",
            final_status="completed",
        )


# ---------------------------------------------------------------------------
# No-op failure slicer
# ---------------------------------------------------------------------------

class _NoOpVoiceBenchSlicer:
    def extract(
        self,
        case: EvaluationCase,
        replay_run: ReplayRun,
        result: BenchmarkResult,
    ) -> list[FailureSlice]:
        return []


assert isinstance(_NoOpVoiceBenchSlicer(), FailureSliceExtractor)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_voicebench(synthetic: bool = True) -> BenchmarkAdapter:
    """Return the VoiceBench BenchmarkAdapter instance.

    Args:
        synthetic: If True (default), use synthetic fixture cases. Set False
            only when HuggingFace VoiceBench data is locally available.
    """
    return BenchmarkAdapter(
        name="voicebench",
        version="v1",
        case_source=VoiceBenchCaseSource(synthetic=synthetic),
        scenario_driver=_SyntheticVoiceBenchDriver(),
        examiner=None,
        metrics=[],
        failure_slicer=_NoOpVoiceBenchSlicer(),
        reporters=[],
    )
