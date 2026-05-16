"""Fixture-pack schema validation (T2 success criterion).

Iterates tests/fixtures/phase_c/*/manifest.json and ground_truth_speakers.json;
asserts required keys are present in each.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "phase_c"

_MANIFEST_REQUIRED_KEYS = {
    "session_id",
    "recording_date",
    "speaker_count",
    "duration_ms",
    "license",
    "redaction_notes",
    "schema_version",
}

_GT_TOP_LEVEL_REQUIRED = {"schema_version", "utterances"}
_GT_UTT_REQUIRED = {"utterance_id", "t_start_ms", "t_end_ms", "speaker_id", "addressed_agent"}


def _session_dirs() -> list[Path]:
    return sorted(d for d in _FIXTURES_ROOT.iterdir() if d.is_dir())


@pytest.mark.parametrize("session_dir", _session_dirs())
def test_manifest_has_required_keys(session_dir: Path) -> None:
    manifest_path = session_dir / "manifest.json"
    assert manifest_path.exists(), f"manifest.json missing in {session_dir.name}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    missing = _MANIFEST_REQUIRED_KEYS - manifest.keys()
    assert not missing, f"{session_dir.name}/manifest.json missing keys: {missing}"


@pytest.mark.parametrize("session_dir", _session_dirs())
def test_ground_truth_has_schema_version(session_dir: Path) -> None:
    gt_path = session_dir / "ground_truth_speakers.json"
    assert gt_path.exists(), f"ground_truth_speakers.json missing in {session_dir.name}"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    missing = _GT_TOP_LEVEL_REQUIRED - gt.keys()
    assert not missing, f"{session_dir.name}/ground_truth_speakers.json missing keys: {missing}"


@pytest.mark.parametrize("session_dir", _session_dirs())
def test_ground_truth_utterances_have_required_fields(session_dir: Path) -> None:
    gt_path = session_dir / "ground_truth_speakers.json"
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    for i, utt in enumerate(gt.get("utterances", [])):
        missing = _GT_UTT_REQUIRED - utt.keys()
        assert not missing, (
            f"{session_dir.name} utterance[{i}] missing fields: {missing}"
        )


def test_fixture_session_count_at_least_three() -> None:
    count = len(_session_dirs())
    assert count >= 3, f"expected >=3 phase_c fixture sessions, got {count}"
