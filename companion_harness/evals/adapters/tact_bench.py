"""TACT-Bench adapter — held-result delivery ("when an always-on agent should stay silent").

Generalized eval-framework adapter (eval-subsystem-spec.md Anchor 5). Built per
docs/plan-tact-bench-minicpm-adapter.md. Only the ScenarioDriver (PR2) is
MiniCPM-coupled; this CaseSource — and the judge/metrics — are model-agnostic.

Scenario definitions are authored upstream in the tact-bench repo
(experiments/vanilla-vs-prompted/scenarios.yaml) and vendored here under
``tact_bench_data/scenarios.yaml`` so the adapter runs CI-hermetically without that
checkout. Keep the two in sync (tact-bench REVISIONS R3).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from companion_harness.schemas import EvaluationCase

_DATA_DIR = Path(__file__).parent / "tact_bench_data"
_DEFAULT_SCENARIOS = _DATA_DIR / "scenarios.yaml"

_BENCH_NAME = "tact_bench"
_BENCH_VERSION = "v1"


@dataclass
class TactCaseSource:
    """Loads the TACT-Bench scenario set into typed ``EvaluationCase``s.

    Model-agnostic: no model SDK import. ``input_mode`` is recorded on each case
    so the (model-specific) driver knows whether to feed audio or text turns.
    """

    scenarios_path: Path = _DEFAULT_SCENARIOS
    input_mode: str = "text"
    name: str = _BENCH_NAME
    version: str = _BENCH_VERSION

    def iter_cases(self, split: str = "all") -> Iterable[EvaluationCase]:
        import yaml  # lazy: keep module import free of the yaml dependency

        data = yaml.safe_load(self.scenarios_path.read_text())
        for scenario in data["scenarios"]:
            yield self._to_case(scenario)

    def _to_case(self, scenario: dict) -> EvaluationCase:
        item = scenario.get("item") or {}
        return EvaluationCase(
            case_id=scenario["id"],
            stage=3,  # Stage 3 — speak/silence policy
            scenario=scenario.get("behavior", scenario["id"]),
            modalities=[self.input_mode],
            fixture_ref=f"tact/{scenario['id']}",
            expected_events=["native_duplex_invocation"],
            expected_metrics={},
            consent_class="safe_eval_fixture",
            benchmark_name=self.name,
            benchmark_version=self.version,
            inputs={
                "user_script": scenario.get("user_script", []),
                "item": item,
                "input_mode": self.input_mode,
                "current_topic": scenario.get("current_topic"),
                "standing_instruction": scenario.get("standing_instruction"),
            },
            expected_behavior={
                "behavior": scenario.get("behavior"),
                "exposes": list(scenario.get("exposes", [])),
                "t_available": item.get("t_available"),
                "becomes_stale_at": item.get("becomes_stale_at"),
                "ground_truth": scenario.get("ground_truth", ""),
            },
            fixtures=[],
        )
