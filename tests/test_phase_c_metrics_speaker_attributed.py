"""Unit tests for Phase C speaker-attributed metrics (T3 success criterion).

Constructs synthetic ReplayRun + ground_truth_speakers.json entirely in-test;
no real diarization required.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from companion_harness.evals.metrics.speaker_attributed import (
    AddressingAccuracyPerSpeakerMetric,
    ReplayMatchRateVsGroundTruthMetric,
    TurnGapMsDistributionPerSpeakerMetric,
)
from companion_harness.evals.schemas import MetricValue
from companion_harness.schemas import ReplayRun


def _make_fixture(tmp_path: Path, utterances: list[dict]) -> Path:
    gt = {"schema_version": "1.0", "session_id": "test_session", "utterances": utterances}
    (tmp_path / "ground_truth_speakers.json").write_text(json.dumps(gt), encoding="utf-8")
    return tmp_path


def _make_event_log(tmp_path: Path, entries: list[dict]) -> Path:
    log_path = tmp_path / "event_log.jsonl"
    with log_path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return log_path


def _make_replay_run(event_log_path: Path) -> ReplayRun:
    return ReplayRun(
        run_id="test_run",
        case_id="test_case",
        implementation_config_version="test",
        policy_version="test",
        started_at="2026-05-16T00:00:00Z",
        finished_at="2026-05-16T00:00:01Z",
        results={},
        failures=[],
        event_log_path=event_log_path,
        timing_mode="synthetic_clock",
        final_status="completed",
    )


# ---------------------------------------------------------------------------
# AddressingAccuracyPerSpeakerMetric
# ---------------------------------------------------------------------------

def test_addressing_accuracy_perfect_match(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "u0", "t_start_ms": 0, "t_end_ms": 1000, "speaker_id": "A", "addressed_agent": True},
        {"utterance_id": "u1", "t_start_ms": 2000, "t_end_ms": 3000, "speaker_id": "B", "addressed_agent": False},
    ]
    fixture_path = _make_fixture(tmp_path, utterances)
    log_path = _make_event_log(tmp_path, [
        {"event_id": "ac0", "event_type": "addressing_classified", "utterance_id": "u0", "current_speaker_id": "A", "caused_by": []},
        {"event_id": "ac1", "event_type": "addressing_classified", "utterance_id": "u1", "current_speaker_id": "B", "caused_by": ["ac0"]},
    ])
    replay_run = _make_replay_run(log_path)
    metric = AddressingAccuracyPerSpeakerMetric(fixture_path=fixture_path)
    result = metric.compute(replay_run)
    assert result.name == "addressing_accuracy_per_speaker"
    assert isinstance(result, MetricValue)
    assert result.value["aggregate"] == 1.0
    assert result.value["per_speaker"]["A"] == 1.0
    assert result.value["per_speaker"]["B"] == 1.0


def test_addressing_accuracy_partial_match(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "u0", "t_start_ms": 0, "t_end_ms": 1000, "speaker_id": "A", "addressed_agent": True},
        {"utterance_id": "u1", "t_start_ms": 2000, "t_end_ms": 3000, "speaker_id": "A", "addressed_agent": True},
    ]
    fixture_path = _make_fixture(tmp_path, utterances)
    # second utterance misidentified as B
    log_path = _make_event_log(tmp_path, [
        {"event_id": "ac0", "event_type": "addressing_classified", "utterance_id": "u0", "current_speaker_id": "A", "caused_by": []},
        {"event_id": "ac1", "event_type": "addressing_classified", "utterance_id": "u1", "current_speaker_id": "B", "caused_by": ["ac0"]},
    ])
    replay_run = _make_replay_run(log_path)
    metric = AddressingAccuracyPerSpeakerMetric(fixture_path=fixture_path)
    result = metric.compute(replay_run)
    assert result.value["per_speaker"]["A"] == 0.5


# ---------------------------------------------------------------------------
# TurnGapMsDistributionPerSpeakerMetric
# ---------------------------------------------------------------------------

def test_turn_gap_distribution_computes(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "u0", "t_start_ms": 0, "t_end_ms": 1000, "speaker_id": "A", "addressed_agent": True},
        {"utterance_id": "u1", "t_start_ms": 3000, "t_end_ms": 4000, "speaker_id": "A", "addressed_agent": True},
    ]
    fixture_path = _make_fixture(tmp_path, utterances)
    log_path = _make_event_log(tmp_path, [])
    replay_run = _make_replay_run(log_path)
    metric = TurnGapMsDistributionPerSpeakerMetric(fixture_path=fixture_path)
    result = metric.compute(replay_run)
    assert result.name == "turn_gap_ms_distribution_per_speaker"
    assert result.unit == "ms"
    assert result.aggregation == "histogram"
    assert "A" in result.value
    assert result.value["A"]["count"] == 1
    assert result.value["A"]["mean_ms"] == 2000.0


def test_turn_gap_distribution_single_utterance_per_speaker(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "u0", "t_start_ms": 0, "t_end_ms": 1000, "speaker_id": "A", "addressed_agent": True},
        {"utterance_id": "u1", "t_start_ms": 2000, "t_end_ms": 3000, "speaker_id": "B", "addressed_agent": False},
    ]
    fixture_path = _make_fixture(tmp_path, utterances)
    log_path = _make_event_log(tmp_path, [])
    replay_run = _make_replay_run(log_path)
    metric = TurnGapMsDistributionPerSpeakerMetric(fixture_path=fixture_path)
    result = metric.compute(replay_run)
    # Only one utterance per speaker → no gaps to compute
    assert "A" not in result.value
    assert "B" not in result.value


# ---------------------------------------------------------------------------
# ReplayMatchRateVsGroundTruthMetric
# ---------------------------------------------------------------------------

def test_replay_match_rate_perfect(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "u0", "t_start_ms": 0, "t_end_ms": 1000, "speaker_id": "A", "addressed_agent": True},
        {"utterance_id": "u1", "t_start_ms": 2000, "t_end_ms": 3000, "speaker_id": "B", "addressed_agent": False},
    ]
    fixture_path = _make_fixture(tmp_path, utterances)
    log_path = _make_event_log(tmp_path, [
        {"event_id": "ac0", "event_type": "addressing_classified", "utterance_id": "u0", "current_speaker_id": "A", "caused_by": []},
        {"event_id": "pd0", "event_type": "policy_decision", "caused_by": ["ac0"], "payload_inline": {"action_type": "full_response", "primary_reason_code": "EOU_CONFIRMED", "supporting_reason_codes": []}},
        {"event_id": "ac1", "event_type": "addressing_classified", "utterance_id": "u1", "current_speaker_id": "B", "caused_by": ["pd0"]},
        {"event_id": "pd1", "event_type": "policy_decision", "caused_by": ["ac1"], "payload_inline": {"action_type": "silence", "primary_reason_code": "NOT_ADDRESSED_TO_AGENT", "supporting_reason_codes": []}},
    ])
    replay_run = _make_replay_run(log_path)
    metric = ReplayMatchRateVsGroundTruthMetric(fixture_path=fixture_path)
    result = metric.compute(replay_run)
    assert result.value["rate"] == 1.0
    assert result.value["matched"] == 2
    assert result.value["total"] == 2
    assert result.value["mismatches"] == []


def test_replay_match_rate_partial(tmp_path: Path) -> None:
    utterances = [
        {"utterance_id": "u0", "t_start_ms": 0, "t_end_ms": 1000, "speaker_id": "A", "addressed_agent": True},
        {"utterance_id": "u1", "t_start_ms": 2000, "t_end_ms": 3000, "speaker_id": "B", "addressed_agent": False},
    ]
    fixture_path = _make_fixture(tmp_path, utterances)
    # Both decisions are silence, but u0 expects addressed → one mismatch
    log_path = _make_event_log(tmp_path, [
        {"event_id": "ac0", "event_type": "addressing_classified", "utterance_id": "u0", "current_speaker_id": "A", "caused_by": []},
        {"event_id": "pd0", "event_type": "policy_decision", "caused_by": ["ac0"], "payload_inline": {"action_type": "silence", "primary_reason_code": "NOT_ADDRESSED_TO_AGENT", "supporting_reason_codes": []}},
        {"event_id": "ac1", "event_type": "addressing_classified", "utterance_id": "u1", "current_speaker_id": "B", "caused_by": ["pd0"]},
        {"event_id": "pd1", "event_type": "policy_decision", "caused_by": ["ac1"], "payload_inline": {"action_type": "silence", "primary_reason_code": "NOT_ADDRESSED_TO_AGENT", "supporting_reason_codes": []}},
    ])
    replay_run = _make_replay_run(log_path)
    metric = ReplayMatchRateVsGroundTruthMetric(fixture_path=fixture_path)
    result = metric.compute(replay_run)
    assert result.value["rate"] == 0.5
    assert result.value["matched"] == 1
    assert len(result.value["mismatches"]) == 1
    assert result.value["mismatches"][0]["utterance_id"] == "u0"


def test_replay_match_rate_all_addressed_action_types_count(tmp_path: Path) -> None:
    """All action types in _ADDRESSED_ACTION_TYPES count as addressed=True matches."""
    from companion_harness.evals.metrics.speaker_attributed import _ADDRESSED_ACTION_TYPES
    for action in _ADDRESSED_ACTION_TYPES:
        tmp_sub = tmp_path / action
        tmp_sub.mkdir()
        utterances = [
            {"utterance_id": "u0", "t_start_ms": 0, "t_end_ms": 1000, "speaker_id": "A", "addressed_agent": True},
        ]
        fp = _make_fixture(tmp_sub, utterances)
        lp = _make_event_log(tmp_sub, [
            {"event_id": "ac0", "event_type": "addressing_classified", "utterance_id": "u0", "current_speaker_id": "A", "caused_by": []},
            {"event_id": "pd0", "event_type": "policy_decision", "caused_by": ["ac0"], "payload_inline": {"action_type": action, "primary_reason_code": "EOU_CONFIRMED", "supporting_reason_codes": []}},
        ])
        rr = _make_replay_run(lp)
        result = ReplayMatchRateVsGroundTruthMetric(fixture_path=fp).compute(rr)
        assert result.value["rate"] == 1.0, f"action_type={action!r} should count as addressed"
