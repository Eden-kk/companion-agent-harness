"""Phase C end-to-end contract test against the canonical diarized fixture.

Exercises LiveExaminerCaseSource → fixture event log → Phase C metrics.
The FixtureScenarioDriver + DiarizationAdapter integration (full live stack)
requires v0.2b's DiarizationAdapter and is gated by @pytest.mark.requires_diarization.

This test validates the Phase C framework against the recorded event logs in
tests/fixtures/phase_c/ without needing a live orchestrator run. The live
diarization path is a separate @pytest.mark.requires_diarization block.

architecture-v0.1.md invariants #1 (no unlogged behavior) and #5 (deterministic
policy replay) are verified by the orphan-check and structural assertions below.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from companion_harness.evals.adapters.live_examiner import LiveExaminerCaseSource
from companion_harness.evals.metrics.speaker_attributed import (
    AddressingAccuracyPerSpeakerMetric,
    ReplayMatchRateVsGroundTruthMetric,
    TurnGapMsDistributionPerSpeakerMetric,
)
from companion_harness.schemas import ReplayRun

_FIXTURES_ROOT = Path(__file__).parent / "fixtures" / "phase_c"
_CANONICAL_SESSION = "phase_c_libripaired_two_speaker_001"


def _make_replay_run(session_dir: Path) -> ReplayRun:
    """Wrap the session's recorded event_log.jsonl as a ReplayRun for metric computation."""
    import time
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return ReplayRun(
        run_id=f"phase_c_{session_dir.name}",
        case_id=session_dir.name,
        implementation_config_version="phase_c-v0.2",
        policy_version="phase_c-v0.2",
        started_at=ts,
        finished_at=ts,
        results={},
        failures=[],
        event_log_path=session_dir / "event_log.jsonl",
        timing_mode="synthetic_clock",
        final_status="completed",
    )


def _all_event_ids(event_log_path: Path) -> set[str]:
    ids = set()
    with event_log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                obj = json.loads(line)
                if "event_id" in obj:
                    ids.add(obj["event_id"])
    return ids


def _orphan_count(event_log_path: Path) -> int:
    """Count caused_by entries that don't resolve to an in-log event_id.

    'session_root' is a well-known virtual root; it is excluded from the orphan check.
    """
    all_ids = _all_event_ids(event_log_path)
    orphans = 0
    with event_log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            for ref in obj.get("caused_by", []):
                if ref != "session_root" and ref not in all_ids:
                    orphans += 1
    return orphans


# ---------------------------------------------------------------------------
# Framework contract (no diarization required — exercises recorded event logs)
# ---------------------------------------------------------------------------

def test_live_examiner_yields_cases_for_all_sessions() -> None:
    """LiveExaminerCaseSource yields cases from all 3 fixture sessions."""
    src = LiveExaminerCaseSource(fixtures_root=_FIXTURES_ROOT)
    cases = list(src.iter_cases("test"))
    # 3 sessions: 4 + 6 + 5 utterances = 15 total
    assert len(cases) == 15
    case_ids = {c.case_id for c in cases}
    assert any("phase_c_synthetic_two_speaker_001" in cid for cid in case_ids)
    assert any("phase_c_synthetic_three_speaker_001" in cid for cid in case_ids)
    assert any("phase_c_libripaired_two_speaker_001" in cid for cid in case_ids)


def test_canonical_session_event_log_is_valid_jsonl() -> None:
    """Event log for canonical fixture parses as valid JSONL."""
    log_path = _FIXTURES_ROOT / _CANONICAL_SESSION / "event_log.jsonl"
    assert log_path.exists()
    with log_path.open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if line:
                try:
                    json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AssertionError(f"line {i+1} not valid JSON: {exc}") from exc


def test_canonical_session_has_addressing_classified_events() -> None:
    """Canonical fixture event log contains addressing_classified events with speaker IDs."""
    log_path = _FIXTURES_ROOT / _CANONICAL_SESSION / "event_log.jsonl"
    ac_events = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                obj = json.loads(line)
                if obj.get("event_type") == "addressing_classified":
                    ac_events.append(obj)
    assert len(ac_events) > 0, "no addressing_classified events in canonical fixture"
    for evt in ac_events:
        assert evt.get("current_speaker_id") is not None, (
            f"addressing_classified event missing current_speaker_id: {evt}"
        )


def test_canonical_session_zero_orphan_events() -> None:
    """All caused_by references in the canonical event log resolve (invariant #1)."""
    log_path = _FIXTURES_ROOT / _CANONICAL_SESSION / "event_log.jsonl"
    assert _orphan_count(log_path) == 0, "orphan event references found"


def test_all_session_event_logs_zero_orphans() -> None:
    """Every fixture session event log has zero orphan event references."""
    for session_dir in sorted(_FIXTURES_ROOT.iterdir()):
        if not session_dir.is_dir():
            continue
        log_path = session_dir / "event_log.jsonl"
        if log_path.exists():
            count = _orphan_count(log_path)
            assert count == 0, f"{session_dir.name}: {count} orphan event references"


def test_phase_c_metrics_compute_on_canonical_session() -> None:
    """All three Phase C metrics compute without error on the canonical fixture."""
    session_dir = _FIXTURES_ROOT / _CANONICAL_SESSION
    replay_run = _make_replay_run(session_dir)

    acc = AddressingAccuracyPerSpeakerMetric(fixture_path=session_dir)
    acc_result = acc.compute(replay_run)
    assert acc_result.name == "addressing_accuracy_per_speaker"
    assert isinstance(acc_result.value, dict)

    gap = TurnGapMsDistributionPerSpeakerMetric(fixture_path=session_dir)
    gap_result = gap.compute(replay_run)
    assert gap_result.name == "turn_gap_ms_distribution_per_speaker"

    rate = ReplayMatchRateVsGroundTruthMetric(fixture_path=session_dir)
    rate_result = rate.compute(replay_run)
    assert rate_result.name == "replay_match_rate_vs_ground_truth"
    assert isinstance(rate_result.value, dict)
    assert "rate" in rate_result.value


def test_replay_match_rate_meets_headline_gate() -> None:
    """Headline gate: replay_match_rate_vs_ground_truth >= 0.7 on canonical fixture."""
    session_dir = _FIXTURES_ROOT / _CANONICAL_SESSION
    replay_run = _make_replay_run(session_dir)
    metric = ReplayMatchRateVsGroundTruthMetric(fixture_path=session_dir)
    result = metric.compute(replay_run)
    rate = result.value["rate"]
    assert rate >= 0.7, (
        f"replay_match_rate_vs_ground_truth={rate} < 0.7 headline gate; "
        f"mismatches={result.value.get('mismatches')}"
    )


# ---------------------------------------------------------------------------
# Live diarization path (requires v0.2b DiarizationAdapter — skip guard)
# ---------------------------------------------------------------------------

@pytest.mark.requires_diarization
def test_live_diarization_path_produces_replay_run() -> None:  # pragma: no cover
    """Full live stack: FixtureScenarioDriver + DiarizationAdapter → ReplayRun.

    Skipped unless @pytest.mark.requires_diarization tests are enabled.
    v0.2b DiarizationAdapter and FixtureScenarioDriver (Phase A.5) are required.
    """
    pytest.skip("requires_diarization: v0.2b DiarizationAdapter not yet merged")
