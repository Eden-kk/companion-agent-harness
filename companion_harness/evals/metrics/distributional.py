"""Distributional timing metrics for CANDOR evaluation (Phase B1).

Each metric consumes a ReplayRun whose ``results`` dict carries timing
observations (populated by CandorCaseSource / the synthetic fixture).
Returns a MetricValue with aggregation="distribution" and a value dict
containing histogram buckets + summary statistics.

Published human-conversation envelopes are drawn from:
  Cao et al., "CANDOR: A Large Scale Dataset of Conversations in the Wild"
  (https://doi.org/10.1126/sciadv.adf3197, 2023).
  Human-normal ranges cited inline per metric.
"""

from __future__ import annotations

import math
from typing import Sequence

from companion_harness.evals.schemas import MetricValue
from companion_harness.schemas import ReplayRun


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _histogram(values: Sequence[float], bucket_width: float) -> dict:
    """Return a compact histogram dict + summary stats."""
    if not values:
        return {"buckets": {}, "count": 0, "mean_ms": None, "p50_ms": None, "p95_ms": None}
    sorted_v = sorted(values)
    n = len(sorted_v)
    buckets: dict[str, int] = {}
    for v in sorted_v:
        label = f"{int(math.floor(v / bucket_width) * bucket_width)}"
        buckets[label] = buckets.get(label, 0) + 1
    mean = sum(sorted_v) / n
    p50 = sorted_v[int(n * 0.50)]
    p95 = sorted_v[min(int(n * 0.95), n - 1)]
    return {
        "buckets": buckets,
        "count": n,
        "mean_ms": round(mean, 1),
        "p50_ms": round(p50, 1),
        "p95_ms": round(p95, 1),
    }


def _extract(replay_run: ReplayRun, key: str) -> list[float]:
    """Pull a list of floats from replay_run.results[key]; return [] if absent."""
    raw = replay_run.results.get(key, [])
    if isinstance(raw, list):
        return [float(x) for x in raw]
    return []


# ---------------------------------------------------------------------------
# Metric implementations
# ---------------------------------------------------------------------------

class TurnGapMsDistribution:
    """Silence between one speaker's turn end and the next speaker's turn start.

    Human normal (CANDOR paper §3.2): median ~200 ms, p95 ~800 ms.
    """

    name = "turn_gap_ms_distribution"

    def compute(self, replay_run: ReplayRun) -> MetricValue:
        values = _extract(replay_run, "turn_gap_ms_observations")
        return MetricValue(
            name=self.name,
            value=_histogram(values, bucket_width=100.0),
            unit="ms",
            aggregation="distribution",
        )


class OverlapMsDistribution:
    """Duration of simultaneous speech (both sides speaking).

    Human normal (CANDOR paper §3.3): median ~150 ms; values > 500 ms are
    relatively rare in natural conversation.
    """

    name = "overlap_ms_distribution"

    def compute(self, replay_run: ReplayRun) -> MetricValue:
        values = _extract(replay_run, "overlap_ms_observations")
        return MetricValue(
            name=self.name,
            value=_histogram(values, bucket_width=50.0),
            unit="ms",
            aggregation="distribution",
        )


class BackchannelPauseDistribution:
    """Pause before / after a backchannel utterance ("mm-hmm", "yeah", etc.).

    Human normal (CANDOR paper §3.4): backchannels cluster < 300 ms after
    partner's prosodic dip; pauses preceding them < 200 ms.
    """

    name = "backchannel_pause_distribution"

    def compute(self, replay_run: ReplayRun) -> MetricValue:
        values = _extract(replay_run, "backchannel_pause_ms_observations")
        return MetricValue(
            name=self.name,
            value=_histogram(values, bucket_width=50.0),
            unit="ms",
            aggregation="distribution",
        )


class ResponseDelayDistribution:
    """Delay from end of user turn to start of agent response.

    Human normal (CANDOR paper §3.2 + §5): median ~300 ms, p95 ~1 200 ms.
    Values > 2 000 ms are perceived as unnaturally long pauses.
    """

    name = "response_delay_distribution"

    def compute(self, replay_run: ReplayRun) -> MetricValue:
        values = _extract(replay_run, "response_delay_ms_observations")
        return MetricValue(
            name=self.name,
            value=_histogram(values, bucket_width=100.0),
            unit="ms",
            aggregation="distribution",
        )


# Convenience list used by CandorAdapter
ALL_DISTRIBUTIONAL_METRICS: list = [
    TurnGapMsDistribution(),
    OverlapMsDistribution(),
    BackchannelPauseDistribution(),
    ResponseDelayDistribution(),
]
