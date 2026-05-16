"""Phase C live-examiner case source (v0.2d).

SYNTHETIC MODE (default):
  Reads per-session directories from ``fixtures_root`` (default
  ``tests/fixtures/phase_c/``). Each session sub-directory must contain
  ``manifest.json`` and ``ground_truth_speakers.json``. One
  ``EvaluationCase`` is yielded per utterance entry.

REAL MODE (opt-in via ``mode="real"``):
  LLM-driven live partner producing utterances in real-time; deferred to
  v0.3. See docs/roadmap-eval-draft.md Phase C v0.3-deferred section.
  Raises ``NotImplementedError`` immediately.

No model SDK imports (eval-subsystem-spec.md Anchor 1).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from companion_harness.evals.protocols import CaseSource
from companion_harness.schemas import EvaluationCase
from companion_harness.speak_policy import POLICY_VERSION

_DEFAULT_FIXTURES_ROOT = Path("tests/fixtures/phase_c")  # Relative; callers should pass absolute paths.


@dataclass
class LiveExaminerCaseSource:
    """Yields per-utterance EvaluationCases from recorded diarized sessions.

    ``name`` and ``version`` are CaseSource Protocol-required class attrs.
    """

    name: str = "live_examiner_phase_c"
    version: str = "0.2"
    fixtures_root: Path = field(default_factory=lambda: _DEFAULT_FIXTURES_ROOT)
    mode: str = "synthetic"

    def __post_init__(self) -> None:
        if self.mode not in {"synthetic", "real"}:
            raise ValueError(f"mode must be 'synthetic' or 'real', got {self.mode!r}")
        if self.mode == "real":
            raise NotImplementedError(
                "Phase C real-mode (live LLM-driven partner) deferred to v0.3; "
                "see docs/roadmap-eval-draft.md Phase C v0.3-deferred section"
            )

    def iter_cases(self, split: str) -> Iterable[EvaluationCase]:
        for session_dir in sorted(self.fixtures_root.iterdir()):
            if not session_dir.is_dir():
                continue
            manifest_path = session_dir / "manifest.json"
            gt_path = session_dir / "ground_truth_speakers.json"
            if not manifest_path.exists() or not gt_path.exists():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            gt = json.loads(gt_path.read_text(encoding="utf-8"))
            session_id = manifest["session_id"]
            utterances = gt.get("utterances", [])
            for utt in utterances:
                utterance_id = utt["utterance_id"]
                yield EvaluationCase(
                    case_id=f"{session_id}_{utterance_id}",
                    stage=0,
                    scenario="live_examiner_phase_c",
                    modalities=["audio"],
                    fixture_ref=str(session_dir),
                    expected_events=[],
                    expected_metrics={
                        "addressed_agent": utt["addressed_agent"],
                        "speaker_id": utt["speaker_id"],
                    },
                    consent_class=manifest.get("license", "safe_eval_fixture"),
                    benchmark_name="live_examiner_phase_c",
                    benchmark_version=POLICY_VERSION,
                    inputs={
                        "utterance_id": utterance_id,
                        "t_start_ms": utt["t_start_ms"],
                        "t_end_ms": utt["t_end_ms"],
                        "speaker_id": utt["speaker_id"],
                        "addressed_agent": utt["addressed_agent"],
                    },
                    expected_behavior={"phase_c": True},
                )


assert isinstance(LiveExaminerCaseSource(), CaseSource)
