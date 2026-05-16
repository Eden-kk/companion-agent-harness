"""CANDOR distributional adapter.

SYNTHETIC MODE (default):
  Real CANDOR data requires the HuggingFace ``datasets`` library and network
  access to download ~50 GB of conversation audio.  Synthetic mode is the
  default: 100 utterance pairs are generated with timing distributions drawn
  from the published human-conversation envelopes in Cao et al. 2023
  (https://doi.org/10.1126/sciadv.adf3197).

  Synthetic-mode lean:
    - turn_gap_ms:          log-normal(mu=5.3, sigma=0.7)  → median ~200 ms
    - overlap_ms:           log-normal(mu=5.0, sigma=0.8)  → median ~148 ms
    - backchannel_pause_ms: log-normal(mu=4.6, sigma=0.7)  → median  ~99 ms
    - response_delay_ms:    log-normal(mu=5.7, sigma=0.6)  → median ~299 ms

  These parameters reproduce the envelope shape from the CANDOR paper
  (§3.2–3.4) without loading real audio. Results are labelled
  "[synthetic]" in all reports.

REAL MODE (opt-in via ``synthetic=False``):
  Streams from the CANDOR HuggingFace dataset (gated; requires HF_TOKEN and
  accepted gate at https://huggingface.co/datasets/ucsb-sobel-lab/CANDOR).
  Audio bytes travel in ``EvaluationCase.inputs['audio_pcm_bytes']`` for
  downstream Phase C examiner use; v0.2c does not decode audio in the driver.

No model SDK imports in this module.

Pinned license: apache-2.0 (CANDOR dataset card as of 2026-01-06).
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass, field
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
# HuggingFace dataset constants — greppable; do not move into __init__ params
# ---------------------------------------------------------------------------

# Canonical dataset slug for the CANDOR conversational audio corpus.
# Gated: requires HF_TOKEN + gate acceptance at the URL above.
_CANDOR_HF_SLUG = "ucsb-sobel-lab/CANDOR"

# Pinned HF commit hash (from "Files and versions" → "History" on the dataset
# page). Update deliberately when you intend to consume a new upstream revision.
# TBD: fill from b200 once gate is accepted — run:
#   python -c "import huggingface_hub as h; [print(c.commit_id, c.title)
#               for c in h.list_repo_commits('ucsb-sobel-lab/CANDOR',
#               repo_type='dataset')]"
_CANDOR_REVISION = "TBD_fill_from_b200_after_gate_accept"

# License pinned from the dataset card; loader refuses if upstream changes it.
_CANDOR_LICENSE = "apache-2.0"

assert _CANDOR_HF_SLUG, "CANDOR HF slug must be set"


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
# Row-level helpers for real mode
# ---------------------------------------------------------------------------

def _candor_stable_key(row: dict) -> tuple:
    return (row.get("speaker_id", ""), row.get("start_time", 0.0))


def _ordered(iterable: Iterable[dict], key, sort_head: int = 100) -> Iterable[dict]:
    """Yield rows with deterministic ordering over the first `sort_head` rows.

    Rows beyond `sort_head` stream in arrival order (bounded buffer; no
    full-corpus materialization per plan Anchor 2).
    """
    head: list[dict] = []
    it = iter(iterable)
    for row in it:
        head.append(row)
        if len(head) >= sort_head:
            break
    yield from sorted(head, key=key)
    yield from it


def _candor_row_to_evaluation_case(row: dict, index: int) -> EvaluationCase:
    """Map one CANDOR streaming row to an EvaluationCase.

    Raises ValueError if required fields are missing (caller increments skip
    counter and continues).
    """
    audio = row.get("audio")
    if audio is None:
        raise ValueError("row missing 'audio' field")
    audio_bytes = audio.get("bytes") or audio.get("array")
    if audio_bytes is None:
        raise ValueError("row 'audio' missing bytes")
    sample_rate = audio.get("sampling_rate", 16000)
    return EvaluationCase(
        case_id=f"candor_real_{index:04d}",
        stage=0,
        scenario="candor_distributional_probe",
        modalities=["audio"],
        fixture_ref=f"candor_real/{index:04d}",
        expected_events=[],
        expected_metrics={},
        consent_class="safe_eval_fixture",
        benchmark_name="candor",
        benchmark_version="v1",
        inputs={
            "audio_pcm_bytes": audio_bytes,
            "sample_rate": sample_rate,
            "turn_gap_ms": float(row.get("turn_gap_ms", 0.0)),
            "overlap_ms": float(row.get("overlap_ms", 0.0)),
            "backchannel_pause_ms": float(row.get("backchannel_pause_ms", 0.0)),
            "response_delay_ms": float(row.get("response_delay_ms", 0.0)),
            "synthetic": False,
        },
        expected_behavior={"distributional": True},
    )


# ---------------------------------------------------------------------------
# CaseSource
# ---------------------------------------------------------------------------

@dataclass
class CandorCaseSource:
    """Yields CANDOR utterance-pair EvaluationCases.

    In synthetic mode (default), generates 100 representative cases using
    timing distributions from Cao et al. 2023 (CANDOR paper §3.2–3.4).

    In real mode (``synthetic=False``), streams from the gated HuggingFace
    dataset at ``_CANDOR_HF_SLUG`` pinned at ``_CANDOR_REVISION``.
    Requires ``HF_TOKEN`` env-var and accepted dataset gate.

    ``name`` and ``version`` are Protocol-required attributes.
    """

    name: str = "candor"
    version: str = "v1"
    synthetic: bool = True
    seed: int = 42
    _attempted: int = field(default=0, init=False, repr=False)
    _skipped: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.synthetic:
            if os.environ.get("HF_TOKEN") is None:
                raise RuntimeError(
                    "HF_TOKEN missing; set HF_TOKEN or run `huggingface-cli login` "
                    "and accept the CANDOR gate at "
                    "https://huggingface.co/datasets/ucsb-sobel-lab/CANDOR."
                )
            if not _CANDOR_REVISION or _CANDOR_REVISION.startswith("TBD"):
                raise RuntimeError(
                    "CANDOR revision hash must be filled; run the command in the comment above on b200."
                )
            from huggingface_hub import dataset_info  # type: ignore[import-not-found]
            info = dataset_info(_CANDOR_HF_SLUG, token=os.environ.get("HF_TOKEN"))
            actual_license = (info.card_data.get("license") or "") if info.card_data else ""
            if actual_license != _CANDOR_LICENSE:
                raise RuntimeError(
                    f"CANDOR upstream license changed: expected {_CANDOR_LICENSE!r}, got {actual_license!r}. "
                    "Review and update _CANDOR_LICENSE before proceeding."
                )

    def iter_cases(self, split: str = "test") -> Iterable[EvaluationCase]:
        if self.synthetic:
            yield from self._synthetic_cases(split)
        else:
            yield from self._real_cases(split)

    def skip_stats(self) -> dict:
        attempted = self._attempted
        skipped = self._skipped
        rate = skipped / attempted if attempted > 0 else 0.0
        return {"attempted": attempted, "skipped": skipped, "skip_rate": rate}

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

    def _real_cases(self, split: str) -> Iterable[EvaluationCase]:
        from datasets import load_dataset  # type: ignore[import-not-found]
        ds = load_dataset(
            _CANDOR_HF_SLUG,
            split=split,
            streaming=True,
            revision=_CANDOR_REVISION,
            token=os.environ.get("HF_TOKEN"),
        )
        self._attempted = 0
        self._skipped = 0
        for index, row in enumerate(_ordered(ds, key=_candor_stable_key, sort_head=100)):
            self._attempted += 1
            try:
                yield _candor_row_to_evaluation_case(row, index)
            except (ValueError, KeyError, TypeError):
                self._skipped += 1


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

    Real-mode rows carry ``audio_pcm_bytes`` in ``inputs``; v0.2c passes the
    case_id reference through results for downstream Phase C consumption without
    inlining the bytes.
    """

    async def run(
        self,
        case: EvaluationCase,
        harness_factory: object,
        run_config: object,
    ) -> ReplayRun:
        started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        inputs = case.inputs or {}
        results: dict = {
            "turn_gap_ms_observations":         [inputs.get("turn_gap_ms", 0.0)],
            "overlap_ms_observations":           [inputs.get("overlap_ms", 0.0)],
            "backchannel_pause_ms_observations": [inputs.get("backchannel_pause_ms", 0.0)],
            "response_delay_ms_observations":    [inputs.get("response_delay_ms", 0.0)],
            "synthetic": inputs.get("synthetic", True),
        }
        if inputs.get("audio_pcm_bytes") is not None:
            results["audio_pcm_bytes_ref"] = case.case_id
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
            when HF_TOKEN is set and the CANDOR gate is accepted.
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
