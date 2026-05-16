"""v0.1g Task 18: thinker-no-direct-speech contract test.

Invariant #2: ThinkerProposalGen emits proposals only — never speech.
Invariant #4: No proactive speech without policy approval.

Assert: in a well-formed event trace, every assistant_audio_buffer_queued event
traces back (via caused_by) to a policy_decision event. No
assistant_audio_buffer_queued event has an unresolvable caused_by chain
(orphan_count == 0).

See docs/roadmap-v0.1e-draft.md §Anchor 2/3 and §Concern C2.
"""

from __future__ import annotations

from companion_harness.causal_graph import CausalGraph
from companion_harness.schemas import Event


def _evt(
    event_id: str,
    event_type: str,
    caused_by: list[str],
    timestamp_mono_ms: int = 0,
) -> Event:
    return Event(
        event_id=event_id,
        session_id="fixture-session",
        schema_version="0.1",
        seq_no=0,
        event_type=event_type,
        timestamp_mono_ms=timestamp_mono_ms,
        timestamp_wall="2026-05-15T00:00:00+00:00",
        source="test",
        caused_by=caused_by,
        payload_hash="abc",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )


def _well_formed_trace() -> list[Event]:
    """A complete causal chain: user speech → policy decision → audio queued."""
    return [
        _evt("usr-001", "user_speech_onset", [], timestamp_mono_ms=0),
        _evt("vad-001", "vad_signal", ["usr-001"], timestamp_mono_ms=10),
        _evt("pol-001", "policy_decision", ["vad-001"], timestamp_mono_ms=20),
        _evt("gen-001", "assistant_generation_start", ["pol-001"], timestamp_mono_ms=30),
        _evt("aud-001", "assistant_audio_buffer_queued", ["gen-001"], timestamp_mono_ms=40),
    ]


def test_well_formed_trace_has_no_orphans():
    """orphan_count == 0 on a complete causal chain (Stage 0 gate)."""
    graph = CausalGraph(_well_formed_trace())
    report = graph.find_orphans()
    assert report.orphan_count == 0, (
        f"unexpected orphans: {report.orphan_event_ids}, dangling: {report.dangling_refs}"
    )


def test_audio_queued_traces_to_policy_decision():
    """Every assistant_audio_buffer_queued is downstream of a policy_decision."""
    trace = _well_formed_trace()
    by_id = {e.event_id: e for e in trace}

    audio_events = [e for e in trace if e.event_type == "assistant_audio_buffer_queued"]
    assert audio_events, "No assistant_audio_buffer_queued in trace"

    policy_ids = {e.event_id for e in trace if e.event_type == "policy_decision"}

    def _reaches_policy(event_id: str, visited: set[str]) -> bool:
        if event_id in visited:
            return False
        visited.add(event_id)
        evt = by_id.get(event_id)
        if evt is None:
            return False
        if evt.event_type == "policy_decision":
            return True
        return any(_reaches_policy(pred, visited) for pred in evt.caused_by)

    for audio_evt in audio_events:
        assert _reaches_policy(audio_evt.event_id, set()), (
            f"{audio_evt.event_id} does not trace back to any policy_decision"
        )


def test_orphan_audio_queued_detected():
    """An assistant_audio_buffer_queued with no valid causal chain IS flagged as orphan."""
    events = [
        _evt("usr-001", "user_speech_onset", []),
        _evt("aud-orphan", "assistant_audio_buffer_queued", ["nonexistent-evt"]),
    ]
    graph = CausalGraph(events)
    report = graph.find_orphans()
    assert report.orphan_count == 1
    assert "aud-orphan" in report.orphan_event_ids


def test_thinker_direct_speech_violation_count_zero():
    """thinker_direct_speech_violation_count gate: zero audio events without policy ancestry."""
    trace = _well_formed_trace()
    graph = CausalGraph(trace)
    report = graph.find_orphans()

    audio_events = [e for e in trace if e.event_type == "assistant_audio_buffer_queued"]
    orphaned_audio = [e for e in audio_events if e.event_id in report.orphan_event_ids]

    thinker_direct_speech_violation_count = len(orphaned_audio)
    assert thinker_direct_speech_violation_count == 0, (
        f"thinker_direct_speech_violation_count = {thinker_direct_speech_violation_count}; gate == 0"
    )


def test_multiple_audio_events_all_trace_to_policy():
    """Multiple assistant_audio_buffer_queued events, all downstream of policy_decision."""
    events = [
        _evt("usr-001", "user_speech_onset", []),
        _evt("vad-001", "vad_signal", ["usr-001"]),
        _evt("pol-001", "policy_decision", ["vad-001"]),
        _evt("gen-001", "assistant_generation_start", ["pol-001"]),
        _evt("aud-001", "assistant_audio_buffer_queued", ["gen-001"]),
        _evt("aud-002", "assistant_audio_buffer_queued", ["gen-001"]),
        _evt("aud-003", "assistant_audio_buffer_queued", ["aud-002"]),
    ]
    graph = CausalGraph(events)
    report = graph.find_orphans()
    assert report.orphan_count == 0


def test_causal_graph_fixture_gate():
    """The causal_graph_001 fixture events are all clean — no orphaned audio."""
    from companion_harness.fixtures.loader import load_fixture

    fixture = load_fixture("causal_graph_001")
    events = [Event(**e) for e in fixture["events"]]

    graph = CausalGraph(events)
    report = graph.find_orphans()

    audio_events = [e for e in events if e.event_type == "assistant_audio_buffer_queued"]
    orphaned_audio = [e for e in audio_events if e.event_id in report.orphan_event_ids]

    assert len(orphaned_audio) == 0, (
        f"orphaned audio events: {[e.event_id for e in orphaned_audio]}"
    )
    assert report.orphan_count == fixture["expected_metrics"]["orphan_action_count"]
