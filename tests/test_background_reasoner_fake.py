"""Contract tests for FakeBackgroundReasoner (v0.1f Task 8).

Success criterion: test_select_and_call_yields_events and
test_summarize_returns_memory_items pass.
"""

from __future__ import annotations

import asyncio

from companion_harness.background_reasoner import FakeBackgroundReasoner, ToolReasonerResult
from companion_harness.schemas import Event, MemoryItem
from companion_harness.tool_router import ToolDispatchRequest


def _run(coro):
    return asyncio.run(coro)


def _make_request() -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name="search_web",
        arguments={"query": "test"},
        caused_by=["req-event-1"],
        routing_hint="smart",
    )


def test_select_and_call_yields_events() -> None:
    async def _inner():
        reasoner = FakeBackgroundReasoner(session_id="sess-1")
        request = _make_request()
        events: list[Event] = []
        async for evt in reasoner.select_and_call(request):
            events.append(evt)
        return events

    events = _run(_inner())

    assert len(events) == 2
    assert events[0].event_type == "background_reasoning_started"
    assert events[1].event_type == "background_reasoning_completed"
    assert "req-event-1" in events[0].caused_by
    assert "req-event-1" in events[1].caused_by
    for evt in events:
        assert evt.event_id
        assert evt.session_id == "sess-1"


def test_summarize_returns_memory_items() -> None:
    reasoner = FakeBackgroundReasoner(session_id="sess-1")
    result = ToolReasonerResult(
        tool_call_id="tcid-abc",
        tool_name="search_web",
        summary_text="Found 3 results about Python.",
        caused_by=["evt-a", "evt-b"],
    )

    items = reasoner.summarize(result)

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, MemoryItem)
    assert item.item_id == "br-tcid-abc"
    assert item.store == "episodic"
    assert item.content["text"] == "Found 3 results about Python."
    assert item.content["tool_name"] == "search_web"
    assert item.source_event_id == "evt-a"
    assert item.user_visible_summary.value == "Found 3 results about Python."
    assert item.valid_from
    assert item.valid_to is None
    assert item.superseded_by is None
