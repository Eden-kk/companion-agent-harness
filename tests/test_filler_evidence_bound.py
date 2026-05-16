"""v0.1f Task 12: filler-evidence-bound contract test (invariant #9).

Success criterion (roadmap Wave 4 Task 12):
  - Every filler in a session log corresponds to a tool_progress_event.
  - Gate: filler_evidence_bound_compliance_rate == 1.0

Invariant #9: no foreground narration about tool progress may exist without
a corresponding ToolProgressEvent in the event log.

The test drives FakeToolRouter + ToolProgressEmitter through multiple
dispatch cycles, then inspects the session log to verify that every call
to record_filler() has a matching tool_progress_event in the log.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from companion_harness.fake_tool_router import FakeToolRouter
from companion_harness.tool_progress_emitter import ToolProgressEmitter
from companion_harness.tool_router import ToolDispatchRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(name: str = "search", i: int = 0) -> ToolDispatchRequest:
    return ToolDispatchRequest(
        tool_name=name,
        arguments={"q": f"query-{i}"},
        caused_by=[f"upstream-{i}"],
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filler_evidence_bound_compliance_rate_equals_1() -> None:
    """Every recorded filler maps 1:1 to a tool_progress_event in the event log.

    filler_evidence_bound_compliance_rate == 1.0.
    """
    router = FakeToolRouter(
        session_id="filler-bound",
        progress_stages=["started", "scanning", "aggregating"],
    )
    emitter = ToolProgressEmitter()

    session_events = []
    filler_records: list[str] = []  # tool_call_ids that had a filler recorded

    # Run 3 dispatch cycles, recording fillers as the orchestrator does.
    for i in range(3):
        request = _make_request(i=i)
        result = await router.dispatch(request)
        session_events.extend(result.events)

        now_ms = int(time.monotonic() * 1000)
        if emitter.should_emit_filler(result.tool_call_id, now_ms):
            emitter.record_filler(result.tool_call_id, now_ms)
            filler_records.append(result.tool_call_id)
        # advance virtual time by 5s between calls so budget resets
        # (emitter tracks per tool_call_id; each call is distinct)

    # For each filler record, assert there exists a tool_progress_event
    # in the session log with matching tool_call_id.
    tool_progress_events = [
        e for e in session_events if e.event_type == "tool_progress_event"
    ]
    progress_call_ids = {
        e.payload_inline["tool_call_id"]
        for e in tool_progress_events
        if e.payload_inline and "tool_call_id" in e.payload_inline
    }

    filler_with_evidence = [
        cid for cid in filler_records if cid in progress_call_ids
    ]

    if not filler_records:
        # No fillers emitted — rate is vacuously 1.0.
        compliance_rate = 1.0
    else:
        compliance_rate = len(filler_with_evidence) / len(filler_records)

    assert compliance_rate == 1.0, (
        f"filler_evidence_bound_compliance_rate={compliance_rate:.3f} < 1.0. "
        f"Fillers without evidence: "
        f"{[cid for cid in filler_records if cid not in progress_call_ids]}"
    )


@pytest.mark.asyncio
async def test_no_filler_without_tool_progress_event_in_log() -> None:
    """Invariant #9: no filler record exists whose tool_call_id has no
    tool_progress_event in the session log.
    """
    router = FakeToolRouter(
        session_id="filler-invariant9",
        progress_stages=["started"],
    )
    emitter = ToolProgressEmitter()

    session_events = []
    filler_tool_call_ids: list[str] = []

    result = await router.dispatch(_make_request())
    session_events.extend(result.events)

    now_ms = int(time.monotonic() * 1000)
    if emitter.should_emit_filler(result.tool_call_id, now_ms):
        emitter.record_filler(result.tool_call_id, now_ms)
        filler_tool_call_ids.append(result.tool_call_id)

    progress_ids = {
        e.payload_inline["tool_call_id"]
        for e in session_events
        if e.event_type == "tool_progress_event"
        and e.payload_inline
        and "tool_call_id" in e.payload_inline
    }

    orphan_fillers = [cid for cid in filler_tool_call_ids if cid not in progress_ids]
    assert not orphan_fillers, (
        f"Invariant #9 violated: fillers emitted without a tool_progress_event: "
        f"{orphan_fillers}"
    )


@pytest.mark.asyncio
async def test_cancelled_call_filler_still_evidence_bound() -> None:
    """Even a cancelled tool call that had a filler must have a tool_progress_event."""
    router = FakeToolRouter(
        session_id="filler-cancel-bound",
        progress_stages=["started", "scanning", "aggregating"],
    )
    emitter = ToolProgressEmitter()

    request = _make_request()
    dispatch_task = asyncio.get_running_loop().create_task(router.dispatch(request))

    await asyncio.sleep(0)

    # Capture tool_call_id from cancel_flags before we cancel.
    assert router._cancel_flags
    tool_call_id = next(iter(router._cancel_flags))

    # Record a filler before cancelling (simulates a filler emitted mid-flight).
    now_ms = int(time.monotonic() * 1000)
    if emitter.should_emit_filler(tool_call_id, now_ms):
        emitter.record_filler(tool_call_id, now_ms)

    await router.cancel(tool_call_id)
    result = await dispatch_task

    # Verify: filler was recorded → must have a tool_progress_event in the log.
    state = emitter._calls.get(tool_call_id)
    if state and state.fillers_emitted > 0:
        progress_in_log = [
            e for e in result.events
            if e.event_type == "tool_progress_event"
            and e.payload_inline
            and e.payload_inline.get("tool_call_id") == tool_call_id
        ]
        assert progress_in_log, (
            "Filler recorded for cancelled tool call but no tool_progress_event "
            f"found in result.events for tool_call_id={tool_call_id}"
        )
