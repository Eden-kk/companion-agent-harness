"""Concrete FailureSliceExtractor — Phase B2.

Given a failed BenchmarkResult + its ReplayRun, traverses the ``caused_by[]``
graph backward from the failed action event to identify the upstream
decision/signal/policy-gate and the adapter that produced it.

Event log format assumed: JSONL, each line a JSON object with at minimum:
  event_id: str
  event_type: str
  caused_by: list[str]
  source: str   (adapter identifier, e.g. "speak_policy", "turn_detector")

If ``replay_run.event_log_path`` is None or unreadable the extractor returns
an empty list (no crash — eval output is advisory).
"""

from __future__ import annotations

import json
from pathlib import Path

from companion_harness.evals.protocols import FailureSliceExtractor
from companion_harness.evals.schemas import BenchmarkResult, FailureSlice
from companion_harness.schemas import EvaluationCase, ReplayRun

# Mapping from event source strings to canonical adapter names used in
# FailureSlice.suspected_adapter.  Unknown sources fall through as-is.
_SOURCE_TO_ADAPTER: dict[str, str] = {
    "speak_policy": "SpeakPolicy",
    "turn_detector": "TurnDetectorSuite",
    "audio_output_controller": "AudioOutputController",
    "vision_sidecar": "VisionSidecar",
    "asr_adapter": "ASRAdapter",
    "foreground_model": "ForegroundModel",
    "event_logger": "EventLogger",
    "benchmark": "BenchmarkRunner",
}

# Event types that represent a decision/gate — traversal stops here.
_DECISION_EVENT_TYPES = {
    "policy_decision",
    "speak_decision",
    "turn_signal",
    "barge_in_signal",
    "silence_decision",
}


def _load_event_graph(event_log_path: Path) -> dict[str, dict]:
    """Load JSONL event log into {event_id: event_dict} index."""
    graph: dict[str, dict] = {}
    try:
        with event_log_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = evt.get("event_id")
                if eid:
                    graph[eid] = evt
    except OSError:
        pass
    return graph


def _walk_caused_by(
    root_event_id: str,
    graph: dict[str, dict],
    max_depth: int = 20,
) -> list[str]:
    """BFS backward over caused_by[] edges; returns list of event_ids visited."""
    visited: list[str] = []
    frontier = [root_event_id]
    seen: set[str] = {root_event_id}
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: list[str] = []
        for eid in frontier:
            visited.append(eid)
            evt = graph.get(eid)
            if evt is None:
                continue
            for parent_id in evt.get("caused_by", []):
                if parent_id not in seen:
                    seen.add(parent_id)
                    next_frontier.append(parent_id)
        frontier = next_frontier
        depth += 1
    return visited


def _find_decision_event(event_ids: list[str], graph: dict[str, dict]) -> dict | None:
    """Return the first event in event_ids whose type is a decision type."""
    for eid in event_ids:
        evt = graph.get(eid)
        if evt and evt.get("event_type") in _DECISION_EVENT_TYPES:
            return evt
    return None


def _infer_adapter(event_ids: list[str], graph: dict[str, dict]) -> str | None:
    """Return canonical adapter name from the first non-benchmark decision event.

    Preference order: decision-type events → any non-benchmark source.
    The root benchmark_case_completed event is always source="benchmark" and
    is skipped first so the causal chain's real adapter surfaces.
    """
    # First pass: prefer events that are decision-type.
    for eid in event_ids:
        evt = graph.get(eid)
        if evt is None:
            continue
        if evt.get("event_type") not in _DECISION_EVENT_TYPES:
            continue
        source = evt.get("source", "")
        if source in _SOURCE_TO_ADAPTER:
            return _SOURCE_TO_ADAPTER[source]
        if source:
            return source
    # Second pass: any non-benchmark source.
    for eid in event_ids:
        evt = graph.get(eid)
        if evt is None:
            continue
        source = evt.get("source", "")
        if source and source != "benchmark":
            return _SOURCE_TO_ADAPTER.get(source, source)
    return None


def _extract_policy_inputs(decision_evt: dict | None) -> dict:
    """Pull decision-time snapshot from event payload_inline if present."""
    if decision_evt is None:
        return {}
    payload = decision_evt.get("payload_inline") or {}
    return {k: v for k, v in payload.items() if k in {
        "action_type", "primary_reason_code", "eou_probability",
        "user_speaking", "assistant_speaking", "policy_version",
    }}


def _suggest_fix(suspected_adapter: str | None, decision_evt: dict | None) -> str | None:
    if suspected_adapter is None:
        return None
    reason = (decision_evt or {}).get("payload_inline", {}).get("primary_reason_code", "")
    if suspected_adapter == "TurnDetectorSuite":
        return "Review TurnDetectorSuite EOU threshold; consider lowering p_done gate."
    if suspected_adapter == "SpeakPolicy":
        if reason:
            return f"SpeakPolicy fired reason_code={reason}; check threshold path for this code."
        return "Inspect SpeakPolicy threshold path for the failing action_type."
    if suspected_adapter == "AudioOutputController":
        return "Check AudioOutputController barge-in latency; verify stop signal propagation."
    if suspected_adapter == "VisionSidecar":
        return "VisionSidecar scene_change_score may be stale; verify frame injection timing."
    return f"Investigate {suspected_adapter} for upstream signal anomaly."


class CausalFailureSliceExtractor:
    """Concrete FailureSliceExtractor.

    For each failed case: loads event log, walks caused_by[] backward from the
    benchmark_case_completed event, identifies the first decision-type event,
    maps its source to a suspected adapter, and returns a FailureSlice.

    Passing cases return an empty list (invariant: no slice for a pass).
    """

    def extract(
        self,
        case: EvaluationCase,
        replay_run: ReplayRun,
        result: BenchmarkResult,
    ) -> list[FailureSlice]:
        if result.pass_:
            return []

        graph: dict[str, dict] = {}
        if replay_run.event_log_path is not None:
            graph = _load_event_graph(Path(replay_run.event_log_path))

        # Find the completed event for this case as the traversal root.
        root_id = f"bcs-{case.case_id}-end"
        if root_id not in graph:
            # Fallback: any benchmark_case_completed event
            for eid, evt in graph.items():
                if evt.get("event_type") == "benchmark_case_completed":
                    root_id = eid
                    break

        causal_ids = _walk_caused_by(root_id, graph) if root_id in graph else []

        decision_evt = _find_decision_event(causal_ids, graph)
        suspected = _infer_adapter(causal_ids, graph)
        policy_inputs = _extract_policy_inputs(decision_evt)
        fix = _suggest_fix(suspected, decision_evt)

        return [
            FailureSlice(
                case_id=case.case_id,
                causal_event_ids=tuple(causal_ids),
                suspected_adapter=suspected,
                relevant_policy_inputs=policy_inputs,
                suggested_fix=fix,
            )
        ]


assert isinstance(CausalFailureSliceExtractor(), FailureSliceExtractor)
