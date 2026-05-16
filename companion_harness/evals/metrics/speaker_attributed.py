"""Speaker-attributed metrics for Phase C live-examiner evaluation (v0.2d).

Three metrics:
  AddressingAccuracyPerSpeakerMetric  -- per-speaker diarization match rate
  TurnGapMsDistributionPerSpeakerMetric -- per-speaker inter-utterance gap histogram
  ReplayMatchRateVsGroundTruthMetric  -- headline: fraction of utterances where the
      harness SpeakDecision matches the ground-truth addressed_agent expectation

``fixture_path: Path`` is injected via __init__ so the Metric Protocol signature
``compute(self, replay_run) -> MetricValue`` is not widened. Caller resolves
``EvaluationCase.fixture_ref`` to a Path before constructing the metric instance;
the metric reads ``fixture_path / "ground_truth_speakers.json"`` at compute() time.

No model SDK imports (eval-subsystem-spec.md Anchor 1).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from companion_harness.evals.schemas import MetricValue
from companion_harness.schemas import ReplayRun

# action_types that count as "addressed" (agent spoke a substantive turn)
_ADDRESSED_ACTION_TYPES = frozenset({
    "full_response",
    "backchannel",
    "short_reaction",
    "clarification",
    "alert",
    "tool_call",
    "tool_status",
    "aesthetic_reaction",
})


def _load_gt(fixture_path: Path) -> list[dict]:
    gt_path = fixture_path / "ground_truth_speakers.json"
    data = json.loads(gt_path.read_text(encoding="utf-8"))
    return data.get("utterances", [])


def _addressing_events(replay_run: ReplayRun) -> list[dict]:
    """Return all addressing_classified events from the event log."""
    log_path = replay_run.event_log_path
    if log_path is None or not log_path.exists():
        return []
    events = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("event_type") == "addressing_classified":
                events.append(obj)
    return events


def _policy_decisions(replay_run: ReplayRun) -> list[dict]:
    """Return all policy_decision events from the event log."""
    log_path = replay_run.event_log_path
    if log_path is None or not log_path.exists():
        return []
    decisions = []
    with log_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("event_type") == "policy_decision":
                decisions.append(obj)
    return decisions


def _histogram(values: list[float], bucket_width: float) -> dict:
    if not values:
        return {"buckets": {}, "count": 0, "mean_ms": None, "p50_ms": None}
    sorted_v = sorted(values)
    n = len(sorted_v)
    buckets: dict[str, int] = {}
    for v in sorted_v:
        label = str(int(math.floor(v / bucket_width) * bucket_width))
        buckets[label] = buckets.get(label, 0) + 1
    return {
        "buckets": buckets,
        "count": n,
        "mean_ms": round(sum(sorted_v) / n, 1),
        "p50_ms": round(sorted_v[int(n * 0.50)], 1),
    }


class AddressingAccuracyPerSpeakerMetric:
    """Fraction of utterances where harness current_speaker_id matches ground truth.

    Returns per-speaker accuracy dict + unweighted mean as ``value``.
    """

    name = "addressing_accuracy_per_speaker"

    def __init__(self, fixture_path: Path) -> None:
        self._fixture_path = fixture_path

    def compute(self, replay_run: ReplayRun) -> MetricValue:
        gt_utterances = _load_gt(self._fixture_path)
        ac_events = _addressing_events(replay_run)

        gt_by_id = {u["utterance_id"]: u for u in gt_utterances}
        ac_by_id = {e["utterance_id"]: e for e in ac_events if "utterance_id" in e}

        per_speaker: dict[str, list[bool]] = {}
        for utt_id, gt in gt_by_id.items():
            spk = gt["speaker_id"]
            ac = ac_by_id.get(utt_id)
            match = ac is not None and ac.get("current_speaker_id") == spk
            per_speaker.setdefault(spk, []).append(match)

        accuracy_by_speaker = {
            spk: round(sum(hits) / len(hits), 4)
            for spk, hits in per_speaker.items()
        }
        aggregate = (
            round(sum(accuracy_by_speaker.values()) / len(accuracy_by_speaker), 4)
            if accuracy_by_speaker else 0.0
        )

        return MetricValue(
            name=self.name,
            value={"aggregate": aggregate, "per_speaker": accuracy_by_speaker},
            unit=None,
            aggregation="scalar",
        )


class TurnGapMsDistributionPerSpeakerMetric:
    """Per-speaker inter-utterance gap histogram from event log timing.

    Gap is computed as t_start_ms[i+1] - t_end_ms[i] for consecutive utterances
    sharing a speaker, read from the ground_truth_speakers.json timing data.
    """

    name = "turn_gap_ms_distribution_per_speaker"

    def __init__(self, fixture_path: Path) -> None:
        self._fixture_path = fixture_path

    def compute(self, replay_run: ReplayRun) -> MetricValue:
        gt_utterances = _load_gt(self._fixture_path)
        sorted_utts = sorted(gt_utterances, key=lambda u: u["t_start_ms"])

        per_speaker: dict[str, list[float]] = {}
        last_by_speaker: dict[str, dict] = {}
        for utt in sorted_utts:
            spk = utt["speaker_id"]
            if spk in last_by_speaker:
                gap = utt["t_start_ms"] - last_by_speaker[spk]["t_end_ms"]
                per_speaker.setdefault(spk, []).append(float(gap))
            last_by_speaker[spk] = utt

        breakdown = {
            spk: _histogram(gaps, bucket_width=500.0)
            for spk, gaps in per_speaker.items()
        }

        return MetricValue(
            name=self.name,
            value=breakdown,
            unit="ms",
            aggregation="histogram",
        )


class ReplayMatchRateVsGroundTruthMetric:
    """Headline Phase C metric: fraction of utterances where SpeakDecision matches GT.

    Match rule:
      - ground_truth addressed_agent=True  → action_type in _ADDRESSED_ACTION_TYPES counts as match
      - ground_truth addressed_agent=False → action_type == "silence" counts as match

    Per-utterance mismatches are surfaced in ``value["mismatches"]`` for downstream
    FailureSliceExtractor consumption (Phase B2 surface; this module does not
    reimplement extraction).
    """

    name = "replay_match_rate_vs_ground_truth"

    def __init__(self, fixture_path: Path) -> None:
        self._fixture_path = fixture_path

    def compute(self, replay_run: ReplayRun) -> MetricValue:
        gt_utterances = _load_gt(self._fixture_path)
        decisions = _policy_decisions(replay_run)

        # Map utterance_id → action_type from the event log.
        # Each policy_decision event follows its addressing_classified event; we
        # read the log in order and pair decisions with utterances by position.
        ac_events = _addressing_events(replay_run)
        utt_id_sequence = [e.get("utterance_id") for e in ac_events if "utterance_id" in e]
        action_by_utt: dict[str, str] = {}
        for utt_id, dec in zip(utt_id_sequence, decisions):
            inline = dec.get("payload_inline") or {}
            action_by_utt[utt_id] = inline.get("action_type", "silence")

        total = len(gt_utterances)
        matched = 0
        mismatches: list[dict] = []
        for utt in gt_utterances:
            utt_id = utt["utterance_id"]
            gt_addressed = utt["addressed_agent"]
            action = action_by_utt.get(utt_id)
            if action is None:
                mismatches.append({"utterance_id": utt_id, "reason": "no_decision_recorded"})
                continue
            if gt_addressed:
                is_match = action in _ADDRESSED_ACTION_TYPES
            else:
                is_match = action == "silence"
            if is_match:
                matched += 1
            else:
                mismatches.append({
                    "utterance_id": utt_id,
                    "gt_addressed": gt_addressed,
                    "action_type": action,
                    "reason": "action_type_mismatch",
                })

        rate = round(matched / total, 4) if total > 0 else 0.0

        return MetricValue(
            name=self.name,
            value={"rate": rate, "matched": matched, "total": total, "mismatches": mismatches},
            unit=None,
            aggregation="scalar",
        )
