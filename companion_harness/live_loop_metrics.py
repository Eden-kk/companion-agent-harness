"""Offline live-loop latency analyzer (v0.1d Task 7 main, v0.1e Task 6 migration).

Pure functions over list[Event].  No I/O, no subprocess, no printing.
Deterministic for a given event list.

Percentile algorithm: nearest-rank, no interpolation.
  p50 = sorted_latencies[ceil(0.50 * n) - 1]
  p95 = sorted_latencies[ceil(0.95 * n) - 1]

status_reason convention (§8):
  Strings starting with "physical_audio_path" are PERMANENT (Task 8 dependency).
  All other reasons are TRANSIENT (more samples or config fix may resolve them).

direct_question_latency status_reason values:
  "trace_dir_not_provided"              — trace_dir is None (no store, action_selected unknown)
  "trace_dir_missing"                   — trace_dir was provided but doesn't exist on disk
  "no_full_response_decisions_in_session" — store present but no full_response decisions found
  "sample_count_below_min: n=N, min=M" — < MIN_SAMPLES_FOR_GATE paired trials
  "ok"                                  — measured, no anomalies
  (may be appended with unpaired/dropped advisory notes)

MIN_SAMPLES_FOR_GATE = 3.  Below this count → NOT_MEASURED.

v0.1e Task 6: action_selected is read from DecisionTrace.counterfactuals via
DecisionTraceStore.read(decision_id).  compute_metrics() accepts an optional
trace_dir: Path; when None, DecisionTrace lookup falls back to None (treating
every policy_decision as not-full-response, producing NOT_MEASURED).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from companion_harness.schemas import Event

__all__ = [
    "MetricStatus",
    "TrialPair",
    "MetricResult",
    "compute_metrics",
]

MetricStatus = Literal["MET", "NOT_MET", "NOT_MEASURED"]

MIN_SAMPLES_FOR_GATE = 3


@dataclass(frozen=True)
class TrialPair:
    """One (start_event_id, end_event_id) pair and its latency in ms."""
    start_event_id: str
    end_event_id: str
    latency_ms: int


@dataclass(frozen=True)
class MetricResult:
    metric_name: str
    p50_ms: int | None
    p95_ms: int | None
    sample_count: int
    status: MetricStatus
    status_reason: str
    trials: list[TrialPair]
    gate_threshold_p50_ms: int | None
    gate_threshold_p95_ms: int | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: list[int], pct: float) -> int:
    n = len(sorted_vals)
    idx = max(0, math.ceil(pct * n) - 1)
    return sorted_vals[idx]


def _sort_key(e: Event) -> tuple[int, str]:
    return (e.timestamp_mono_ms, e.event_id)


def _sort_events(events: list[Event]) -> list[Event]:
    return sorted(events, key=_sort_key)


def _build_index(events: list[Event]) -> dict[str, Event]:
    return {e.event_id: e for e in events}


def _causal_ancestors(event_id: str, index: dict[str, Event], depth_limit: int = 64) -> set[str]:
    """Return the set of all ancestor event_ids reachable via caused_by chains."""
    visited: set[str] = set()
    stack = [event_id]
    while stack and len(visited) < depth_limit:
        eid = stack.pop()
        if eid in visited:
            continue
        visited.add(eid)
        evt = index.get(eid)
        if evt is not None:
            stack.extend(evt.caused_by)
    return visited


def _split_sessions(events: list[Event]) -> list[list[Event]]:
    """Split event list on harness_init boundaries (primary); orchestrator_started fallback."""
    sorted_evts = _sort_events(events)
    has_harness_init = any(e.event_type == "harness_init" for e in sorted_evts)
    boundary_type = "harness_init" if has_harness_init else "orchestrator_started"

    sessions: list[list[Event]] = []
    current: list[Event] = []
    for e in sorted_evts:
        if e.event_type == boundary_type and current:
            sessions.append(current)
            current = []
        current.append(e)
    if current:
        sessions.append(current)
    return sessions


# ---------------------------------------------------------------------------
# Per-metric computers
# ---------------------------------------------------------------------------

def _load_action_selected(policy_evt: Event, store: Any) -> str | None:
    """Return counterfactuals['action_selected'] for a policy_decision event, or None."""
    if store is None or policy_evt.payload_ref is None:
        return None
    if not policy_evt.payload_ref.startswith("decision_trace://"):
        return None
    decision_id = policy_evt.payload_ref[len("decision_trace://"):]
    try:
        trace = store.read(decision_id)
        return trace.counterfactuals.get("action_selected")
    except FileNotFoundError:
        return None


def _make_store(trace_dir: Path | None) -> Any:
    if trace_dir is None or not trace_dir.exists():
        return None
    from companion_harness.decision_trace_store import DecisionTraceStore
    return DecisionTraceStore(trace_dir)


def _no_fr_reason(store: Any, trace_dir: Path | None) -> str:
    """Return the correct status_reason when no full_response decisions are found."""
    if store is None:
        if trace_dir is not None:
            return "trace_dir_missing"
        return "trace_dir_not_provided"
    return "no_full_response_decisions_in_session"


def _compute_direct_question_latency(
    events: list[Event],
    trace_dir: Path | None = None,
) -> MetricResult:
    """Start: policy_decision where action_selected=='full_response'; end: first causally-downstream
    assistant_audio_buffer_flushed that does NOT have a backchannel-decision in its causal chain."""
    sorted_evts = _sort_events(events)
    index = _build_index(events)
    store = _make_store(trace_dir)

    # Identify policy_decision events whose trace has action_selected == "full_response".
    # Also identify those with action_selected == "backchannel" for causal-chain exclusion.
    pd_evts = [e for e in sorted_evts if e.event_type == "policy_decision"]
    full_response_pd_ids: set[str] = set()
    backchannel_pd_ids: set[str] = set()
    for pd in pd_evts:
        action = _load_action_selected(pd, store)
        if action == "full_response":
            full_response_pd_ids.add(pd.event_id)
        elif action == "backchannel":
            backchannel_pd_ids.add(pd.event_id)

    full_response_evts = [e for e in pd_evts if e.event_id in full_response_pd_ids]
    if not full_response_evts:
        return MetricResult(
            metric_name="direct_question_latency",
            p50_ms=None,
            p95_ms=None,
            sample_count=0,
            status="NOT_MEASURED",
            status_reason=_no_fr_reason(store, trace_dir),
            trials=[],
            gate_threshold_p50_ms=800,
            gate_threshold_p95_ms=1500,
        )

    flush_evts = [e for e in sorted_evts if e.event_type == "assistant_audio_buffer_flushed"]

    trials: list[TrialPair] = []
    advisory_notes: list[str] = []
    unpaired_count = 0
    dropped_count = 0

    # Track which flush events have already been matched (edge case 5: multiple flushes → first only)
    matched_flush_ids: set[str] = set()

    for fr_evt in full_response_evts:
        policy_decision_id = fr_evt.event_id

        paired_flush: Event | None = None
        for flush in flush_evts:
            if flush.event_id in matched_flush_ids:
                continue
            if flush.timestamp_mono_ms < fr_evt.timestamp_mono_ms:
                continue
            ancestors = _causal_ancestors(flush.event_id, index)
            if policy_decision_id not in ancestors:
                continue
            # Exclude backchannel-derived flushes (edge case 3)
            bc_in_chain = any(aid in backchannel_pd_ids for aid in ancestors)
            if bc_in_chain:
                continue
            paired_flush = flush
            break

        if paired_flush is None:
            unpaired_count += 1
            continue

        # Non-monotonic timestamp check (edge case 4)
        if paired_flush.timestamp_mono_ms < fr_evt.timestamp_mono_ms:
            advisory_notes.append(f"dropped_trial: timestamp_regression at {fr_evt.event_id}")
            dropped_count += 1
            continue

        matched_flush_ids.add(paired_flush.event_id)
        latency = paired_flush.timestamp_mono_ms - fr_evt.timestamp_mono_ms
        trials.append(TrialPair(
            start_event_id=fr_evt.event_id,
            end_event_id=paired_flush.event_id,
            latency_ms=latency,
        ))

    reason_parts: list[str] = []
    if unpaired_count > 0:
        reason_parts.append(f"unpaired_full_response_decisions: n={unpaired_count}")
    if dropped_count > 0:
        for note in advisory_notes:
            reason_parts.append(note)

    n = len(trials)
    if n < MIN_SAMPLES_FOR_GATE:
        reason = f"sample_count_below_min: n={n}, min={MIN_SAMPLES_FOR_GATE}"
        if reason_parts:
            reason = reason + "; " + "; ".join(reason_parts)
        sorted_lats = sorted(t.latency_ms for t in trials)
        p50 = _percentile(sorted_lats, 0.50) if sorted_lats else None
        p95 = _percentile(sorted_lats, 0.95) if sorted_lats else None
        return MetricResult(
            metric_name="direct_question_latency",
            p50_ms=p50,
            p95_ms=p95,
            sample_count=n,
            status="NOT_MEASURED",
            status_reason=reason,
            trials=trials,
            gate_threshold_p50_ms=800,
            gate_threshold_p95_ms=1500,
        )

    sorted_lats = sorted(t.latency_ms for t in trials)
    p50 = _percentile(sorted_lats, 0.50)
    p95 = _percentile(sorted_lats, 0.95)

    met = p50 < 800 and p95 < 1500
    status: MetricStatus = "MET" if met else "NOT_MET"
    reason = "ok" if not reason_parts else "; ".join(reason_parts)
    return MetricResult(
        metric_name="direct_question_latency",
        p50_ms=p50,
        p95_ms=p95,
        sample_count=n,
        status=status,
        status_reason=reason,
        trials=trials,
        gate_threshold_p50_ms=800,
        gate_threshold_p95_ms=1500,
    )


def _compute_vad_to_stop(events: list[Event]) -> MetricResult:
    """Start: vad_user_speech_onset (only when generation in flight);
    end: assistant_audio_stop_completed OR assistant_generation_cancel_requested, whichever first."""
    sorted_evts = _sort_events(events)

    # Track generation state: open on assistant_generation_start, close on flush/stop/cancel
    generation_open = False
    last_generation_start_id: str | None = None
    onset_waiting: Event | None = None  # vad onset pending a stop event

    trials: list[TrialPair] = []
    advisory_notes: list[str] = []
    dropped_count = 0

    STOP_TYPES = {"assistant_audio_stop_completed", "assistant_generation_cancel_requested"}

    for e in sorted_evts:
        if e.event_type == "assistant_generation_start":
            generation_open = True
            last_generation_start_id = e.event_id
            onset_waiting = None

        elif e.event_type == "assistant_audio_buffer_flushed":
            generation_open = False
            onset_waiting = None

        elif e.event_type == "assistant_audio_stop_completed" or e.event_type == "assistant_generation_cancel_requested":
            generation_open = False
            if onset_waiting is not None:
                start = onset_waiting
                onset_waiting = None
                if e.timestamp_mono_ms < start.timestamp_mono_ms:
                    advisory_notes.append(f"dropped_trial: timestamp_regression at {start.event_id}")
                    dropped_count += 1
                else:
                    trials.append(TrialPair(
                        start_event_id=start.event_id,
                        end_event_id=e.event_id,
                        latency_ms=e.timestamp_mono_ms - start.timestamp_mono_ms,
                    ))
            # else: stop with no preceding onset — skip (edge case 7)

        elif e.event_type == "vad_user_speech_onset":
            if generation_open:
                # Begin a barge-in trial
                onset_waiting = e
            # else: no generation in flight — skip (edge case 6)

    n = len(trials)
    if n < MIN_SAMPLES_FOR_GATE:
        reason = f"sample_count_below_min: n={n}, min={MIN_SAMPLES_FOR_GATE}"
        sorted_lats = sorted(t.latency_ms for t in trials)
        p50 = _percentile(sorted_lats, 0.50) if sorted_lats else None
        p95 = _percentile(sorted_lats, 0.95) if sorted_lats else None
        return MetricResult(
            metric_name="vad_detected_user_speech_to_stop_ms",
            p50_ms=p50,
            p95_ms=p95,
            sample_count=n,
            status="NOT_MEASURED",
            status_reason=reason,
            trials=trials,
            gate_threshold_p50_ms=None,
            gate_threshold_p95_ms=200,
        )

    sorted_lats = sorted(t.latency_ms for t in trials)
    p50 = _percentile(sorted_lats, 0.50)
    p95 = _percentile(sorted_lats, 0.95)
    met = p95 < 200
    status: MetricStatus = "MET" if met else "NOT_MET"
    return MetricResult(
        metric_name="vad_detected_user_speech_to_stop_ms",
        p50_ms=p50,
        p95_ms=p95,
        sample_count=n,
        status=status,
        status_reason="ok",
        trials=trials,
        gate_threshold_p50_ms=None,
        gate_threshold_p95_ms=200,
    )


def _compute_physical_onset_to_stop(_events: list[Event]) -> MetricResult:
    return MetricResult(
        metric_name="physical_user_speech_onset_to_stop_ms",
        p50_ms=None,
        p95_ms=None,
        sample_count=0,
        status="NOT_MEASURED",
        status_reason="physical_audio_path_not_wired",
        trials=[],
        gate_threshold_p50_ms=None,
        gate_threshold_p95_ms=350,
    )


def _compute_end_to_end_latency(events: list[Event]) -> MetricResult:
    """Start: first harness_init (primary) or orchestrator_started (fallback);
    end: first assistant_audio_buffer_flushed.  One trial per session."""
    sorted_evts = _sort_events(events)

    anchor: Event | None = None
    for e in sorted_evts:
        if e.event_type == "harness_init":
            anchor = e
            break
    if anchor is None:
        for e in sorted_evts:
            if e.event_type == "orchestrator_started":
                anchor = e
                break

    if anchor is None:
        return MetricResult(
            metric_name="end_to_end_response_latency",
            p50_ms=None,
            p95_ms=None,
            sample_count=0,
            status="NOT_MEASURED",
            status_reason="no_session_anchor_event",
            trials=[],
            gate_threshold_p50_ms=None,
            gate_threshold_p95_ms=None,
        )

    flush: Event | None = None
    for e in sorted_evts:
        if e.event_type == "assistant_audio_buffer_flushed":
            flush = e
            break

    if flush is None:
        return MetricResult(
            metric_name="end_to_end_response_latency",
            p50_ms=None,
            p95_ms=None,
            sample_count=0,
            status="NOT_MEASURED",
            status_reason="no_assistant_audio_emitted",
            trials=[],
            gate_threshold_p50_ms=None,
            gate_threshold_p95_ms=None,
        )

    latency = flush.timestamp_mono_ms - anchor.timestamp_mono_ms
    if latency < 0:
        return MetricResult(
            metric_name="end_to_end_response_latency",
            p50_ms=None,
            p95_ms=None,
            sample_count=0,
            status="NOT_MEASURED",
            status_reason=f"dropped_trial: timestamp_regression at {anchor.event_id}",
            trials=[],
            gate_threshold_p50_ms=None,
            gate_threshold_p95_ms=None,
        )

    trial = TrialPair(
        start_event_id=anchor.event_id,
        end_event_id=flush.event_id,
        latency_ms=latency,
    )
    # Observational: always MET (not gated)
    return MetricResult(
        metric_name="end_to_end_response_latency",
        p50_ms=latency,
        p95_ms=latency,
        sample_count=1,
        status="MET",
        status_reason="observational",
        trials=[trial],
        gate_threshold_p50_ms=None,
        gate_threshold_p95_ms=None,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_metrics(
    events: list[Event],
    trace_dir: Path | None = None,
) -> dict[str, MetricResult]:
    """Compute the full §3 metric table from an event log.

    Returns a dict keyed by metric_name:
      "direct_question_latency"
      "vad_detected_user_speech_to_stop_ms"
      "physical_user_speech_onset_to_stop_ms"
      "end_to_end_response_latency"

    Multi-session logs (split on harness_init boundaries) compute each metric
    per session; results are aggregated across sessions (trials pooled).

    trace_dir: directory containing DecisionTrace JSON files produced by
    DecisionTraceStore.write().  Required for direct_question_latency to
    distinguish full_response from other action types.  When None, that metric
    returns NOT_MEASURED.
    """
    sessions = _split_sessions(events)

    # Pool trials across sessions for direct_question_latency and vad_to_stop.
    # end_to_end: one trial per session (first anchor → first flush).
    all_dql_trials: list[TrialPair] = []
    all_vad_trials: list[TrialPair] = []
    all_e2e_trials: list[TrialPair] = []

    dql_advisory: list[str] = []
    dql_unpaired = 0
    dql_no_fr_sessions = 0

    for sess_events in sessions:
        dql = _compute_direct_question_latency(sess_events, trace_dir)
        _no_fr_reasons = {"no_full_response_decisions_in_session", "trace_dir_not_provided", "trace_dir_missing"}
        if dql.status_reason in _no_fr_reasons:
            dql_no_fr_sessions += 1
        else:
            all_dql_trials.extend(dql.trials)
            # Extract unpaired count from advisory in status_reason
            if "unpaired_full_response_decisions:" in dql.status_reason:
                for part in dql.status_reason.split(";"):
                    part = part.strip()
                    if part.startswith("unpaired_full_response_decisions:"):
                        try:
                            dql_unpaired += int(part.split("n=")[1].split(",")[0])
                        except (IndexError, ValueError):
                            pass

        vad = _compute_vad_to_stop(sess_events)
        all_vad_trials.extend(vad.trials)

        e2e = _compute_end_to_end_latency(sess_events)
        all_e2e_trials.extend(e2e.trials)

    # Re-compute aggregated direct_question_latency
    _store = _make_store(trace_dir)
    total_fr_decisions = sum(
        1 for e in events
        if e.event_type == "policy_decision"
        and _load_action_selected(e, _store) == "full_response"
    )
    if total_fr_decisions == 0:
        dql_result = MetricResult(
            metric_name="direct_question_latency",
            p50_ms=None,
            p95_ms=None,
            sample_count=0,
            status="NOT_MEASURED",
            status_reason=_no_fr_reason(_store, trace_dir),
            trials=[],
            gate_threshold_p50_ms=800,
            gate_threshold_p95_ms=1500,
        )
    else:
        n = len(all_dql_trials)
        reason_parts: list[str] = []
        if dql_unpaired > 0:
            reason_parts.append(f"unpaired_full_response_decisions: n={dql_unpaired}")
        if n < MIN_SAMPLES_FOR_GATE:
            reason = f"sample_count_below_min: n={n}, min={MIN_SAMPLES_FOR_GATE}"
            if reason_parts:
                reason = reason + "; " + "; ".join(reason_parts)
            sorted_lats = sorted(t.latency_ms for t in all_dql_trials)
            dql_result = MetricResult(
                metric_name="direct_question_latency",
                p50_ms=_percentile(sorted_lats, 0.50) if sorted_lats else None,
                p95_ms=_percentile(sorted_lats, 0.95) if sorted_lats else None,
                sample_count=n,
                status="NOT_MEASURED",
                status_reason=reason,
                trials=all_dql_trials,
                gate_threshold_p50_ms=800,
                gate_threshold_p95_ms=1500,
            )
        else:
            sorted_lats = sorted(t.latency_ms for t in all_dql_trials)
            p50 = _percentile(sorted_lats, 0.50)
            p95 = _percentile(sorted_lats, 0.95)
            met = p50 < 800 and p95 < 1500
            status: MetricStatus = "MET" if met else "NOT_MET"
            reason = "ok" if not reason_parts else "; ".join(reason_parts)
            dql_result = MetricResult(
                metric_name="direct_question_latency",
                p50_ms=p50,
                p95_ms=p95,
                sample_count=n,
                status=status,
                status_reason=reason,
                trials=all_dql_trials,
                gate_threshold_p50_ms=800,
                gate_threshold_p95_ms=1500,
            )

    # Re-compute aggregated vad_to_stop
    n_vad = len(all_vad_trials)
    if n_vad < MIN_SAMPLES_FOR_GATE:
        sorted_lats_v = sorted(t.latency_ms for t in all_vad_trials)
        vad_result = MetricResult(
            metric_name="vad_detected_user_speech_to_stop_ms",
            p50_ms=_percentile(sorted_lats_v, 0.50) if sorted_lats_v else None,
            p95_ms=_percentile(sorted_lats_v, 0.95) if sorted_lats_v else None,
            sample_count=n_vad,
            status="NOT_MEASURED",
            status_reason=f"sample_count_below_min: n={n_vad}, min={MIN_SAMPLES_FOR_GATE}",
            trials=all_vad_trials,
            gate_threshold_p50_ms=None,
            gate_threshold_p95_ms=200,
        )
    else:
        sorted_lats_v = sorted(t.latency_ms for t in all_vad_trials)
        p50_v = _percentile(sorted_lats_v, 0.50)
        p95_v = _percentile(sorted_lats_v, 0.95)
        met_v = p95_v < 200
        vad_result = MetricResult(
            metric_name="vad_detected_user_speech_to_stop_ms",
            p50_ms=p50_v,
            p95_ms=p95_v,
            sample_count=n_vad,
            status="MET" if met_v else "NOT_MET",
            status_reason="ok",
            trials=all_vad_trials,
            gate_threshold_p50_ms=None,
            gate_threshold_p95_ms=200,
        )

    # end_to_end: use first session's trial only (one trial per session, report first)
    if all_e2e_trials:
        t = all_e2e_trials[0]
        e2e_result = MetricResult(
            metric_name="end_to_end_response_latency",
            p50_ms=t.latency_ms,
            p95_ms=t.latency_ms,
            sample_count=1,
            status="MET",
            status_reason="observational",
            trials=all_e2e_trials,
            gate_threshold_p50_ms=None,
            gate_threshold_p95_ms=None,
        )
    else:
        e2e_result = _compute_end_to_end_latency(events)

    return {
        "direct_question_latency": dql_result,
        "vad_detected_user_speech_to_stop_ms": vad_result,
        "physical_user_speech_onset_to_stop_ms": _compute_physical_onset_to_stop(events),
        "end_to_end_response_latency": e2e_result,
    }
