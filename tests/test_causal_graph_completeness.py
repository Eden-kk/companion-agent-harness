"""Stage 0 — reconstructed DAG has no orphan actions.

See docs/architecture-v0.1.md §Part 6 Stage 0 and §Part 8 v0.1a acceptance gate
(orphan_action_count = 0). Every action must trace to user input or a
scheduled trigger.
"""

import pytest

from companion_harness.causal_graph import CausalGraph, OrphanReport
from companion_harness.schemas import Event


def _evt(event_id: str, caused_by: list[str], event_type: str = "signal") -> Event:
    return Event(
        event_id=event_id,
        session_id="test-session",
        schema_version="0.1",
        seq_no=0,
        event_type=event_type,
        timestamp_mono_ms=0,
        timestamp_wall="",
        source="test",
        caused_by=caused_by,
        payload_hash="",
        payload_ref=None,
        payload_kind="signal",
        subject_class="self",
        sensitivity="safe",
        retention_policy_id="default",
    )


def test_causal_graph_completeness():
    """Orphan detector finds zero orphans on a well-formed synthetic trace."""
    root = _evt("evt-1", [])                        # declared root (user input)
    child = _evt("evt-2", ["evt-1"])                # caused by root
    grandchild = _evt("evt-3", ["evt-1", "evt-2"])  # caused by both

    graph = CausalGraph([root, child, grandchild])
    report = graph.find_orphans()

    assert report.orphan_count == 0, f"unexpected orphans: {report.orphan_event_ids}"


def test_orphan_detected_on_missing_predecessor():
    """Orphan detector flags an event whose caused_by ref is not in the trace."""
    root = _evt("evt-1", [])
    orphan = _evt("evt-2", ["evt-missing"])  # "evt-missing" not in graph

    graph = CausalGraph([root, orphan])
    report = graph.find_orphans()

    assert report.orphan_count == 1
    assert "evt-2" in report.orphan_event_ids
    assert report.dangling_refs["evt-2"] == ["evt-missing"]


def test_log_drop_or_degrade_is_not_an_orphan():
    """log_drop_or_degrade with _dropped_before_enqueue sentinel must NOT be flagged."""
    root = _evt("evt-1", [])
    degrade = _evt(
        "degrade-1",
        ["_dropped_before_enqueue"],
        event_type="log_drop_or_degrade",
    )

    graph = CausalGraph([root, degrade])
    report = graph.find_orphans()

    assert report.orphan_count == 0, f"log_drop_or_degrade wrongly flagged: {report}"


def test_sentinel_in_non_root_event_type_is_not_an_orphan():
    """_dropped_before_enqueue sentinel is always valid in any event's caused_by."""
    root = _evt("evt-1", [])
    evt = _evt("evt-2", ["_dropped_before_enqueue"])

    graph = CausalGraph([root, evt])
    report = graph.find_orphans()

    assert report.orphan_count == 0


def test_empty_trace_has_no_orphans():
    """An empty event list produces an OrphanReport with zero orphans."""
    graph = CausalGraph([])
    report = graph.find_orphans()
    assert report.orphan_count == 0


def test_duplicate_event_id_raises():
    """CausalGraph raises ValueError when two events share the same event_id."""
    evt = _evt("evt-1", [])
    with pytest.raises(ValueError, match="evt-1"):
        CausalGraph([evt, evt])
