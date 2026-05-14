"""Offline causal-graph reconstruction from caused_by[] edges on Events.

See docs/architecture-v0.1.md §Part 6 Stage 0 — test_causal_graph_completeness
requires that every action traces to either user input or a scheduled trigger;
orphan events are a Stage 0 contract failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from companion_harness.schemas import Event

__all__ = ["CausalGraph", "OrphanReport"]

# Sentinel values that are valid in caused_by[] but are not real event_ids.
_KNOWN_SENTINELS: frozenset[str] = frozenset({"_dropped_before_enqueue"})

# event_types that are legitimate DAG roots even when caused_by is non-empty.
_ROOT_EVENT_TYPES: frozenset[str] = frozenset({"log_drop_or_degrade"})


@dataclass
class OrphanReport:
    orphan_event_ids: list[str]          # events with unresolvable causal edges
    dangling_refs: dict[str, list[str]]  # event_id -> unresolvable caused_by values

    @property
    def orphan_count(self) -> int:
        return len(self.orphan_event_ids)


class CausalGraph:
    """Reconstruct a session's causal DAG from a flat list of Events."""

    def __init__(self, events: list[Event]) -> None:
        self._by_id: dict[str, Event] = {e.event_id: e for e in events}

    def find_orphans(self) -> OrphanReport:
        """Return every event that has at least one unresolvable causal predecessor.

        An event is an orphan when any entry in caused_by[] is:
          - not a known event_id in this graph, AND
          - not a known sentinel string.

        Exceptions:
          - caused_by == [] means the event is a declared root (user input or
            scheduled trigger) — not an orphan.
          - event_type == "log_drop_or_degrade" is a valid root regardless of
            its caused_by contents; sentinels like "_dropped_before_enqueue"
            are expected and must not be flagged.
        """
        known_ids = set(self._by_id.keys())
        orphan_ids: list[str] = []
        dangling: dict[str, list[str]] = {}

        for event in self._by_id.values():
            if event.event_type in _ROOT_EVENT_TYPES:
                continue  # valid root; sentinels in caused_by are expected

            unresolvable = [
                ref for ref in event.caused_by
                if ref not in known_ids and ref not in _KNOWN_SENTINELS
            ]
            if unresolvable:
                orphan_ids.append(event.event_id)
                dangling[event.event_id] = unresolvable

        return OrphanReport(orphan_event_ids=orphan_ids, dangling_refs=dangling)
