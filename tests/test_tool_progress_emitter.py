"""Tests for ToolProgressEmitter (v0.1f Task 4).

Success criterion: pytest tests/test_tool_progress_emitter.py -v  →  5 passed.

Covers:
  - filler budget caps at 2 per call
  - 4 000 ms minimum gap between fillers
  - evidence_at() is replay-deterministic (no wall-clock dependency)
  - silence_won_already set after first filler when gap not cleared
  - ProgressStage Literal alphabet is the exact 5-value set
"""

from __future__ import annotations

from typing import get_args

import pytest

from companion_harness.tool_progress import ProgressStage, ToolProgressEvidence
from companion_harness.tool_progress_emitter import ToolProgressEmitter


# ---------------------------------------------------------------------------
# Minimal Event stub — avoids constructing a full schemas.Event
# ---------------------------------------------------------------------------

class _StubEvent:
    def __init__(self, event_type: str, tool_call_id: str,
                 timestamp_mono_ms: int, progress_stage: str | None = None):
        self.event_type = event_type
        self.timestamp_mono_ms = timestamp_mono_ms
        self.payload_inline: dict = {"tool_call_id": tool_call_id}
        if progress_stage is not None:
            self.payload_inline["progress_stage"] = progress_stage


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_filler_budget_caps_at_2_per_call():
    emitter = ToolProgressEmitter()
    cid = "call-001"
    t = 0

    assert emitter.should_emit_filler(cid, t) is True
    emitter.record_filler(cid, t)

    t += 5_000
    assert emitter.should_emit_filler(cid, t) is True
    emitter.record_filler(cid, t)

    t += 5_000
    assert emitter.should_emit_filler(cid, t) is False


def test_filler_min_4s_spacing():
    # Check gap enforcement: before the gap closes → False
    emitter_a = ToolProgressEmitter()
    cid = "call-002a"
    emitter_a.record_filler(cid, 0)
    assert emitter_a.should_emit_filler(cid, 3_999) is False

    # After the gap clears → True (fresh emitter so silence_won_already not set)
    emitter_b = ToolProgressEmitter()
    cid2 = "call-002b"
    emitter_b.record_filler(cid2, 0)
    assert emitter_b.should_emit_filler(cid2, 4_000) is True


def test_evidence_at_pure_function_replay_deterministic():
    emitter = ToolProgressEmitter()
    cid = "call-003"

    events = [
        _StubEvent("tool_progress_event", cid, 1_000, "scanning"),
        _StubEvent("tool_progress_event", cid, 3_000, "aggregating"),
    ]

    result_a = emitter.evidence_at(cid, 5_000, iter(events))
    result_b = emitter.evidence_at(cid, 5_000, iter(events))

    assert result_a == result_b
    assert result_a.ms_since_last_filler == 2_000
    assert result_a.progress_stage == "aggregating"


def test_silence_wins_after_first_filler():
    emitter = ToolProgressEmitter()
    cid = "call-004"

    emitter.record_filler(cid, 0)
    # Second attempt within 4 000 ms should set silence_won_already
    assert emitter.should_emit_filler(cid, 1_000) is False
    assert emitter._calls[cid].silence_won_already is True

    # Subsequent calls are also blocked
    assert emitter.should_emit_filler(cid, 10_000) is False


def test_progress_stage_alphabet_pinned():
    expected = {"started", "scanning", "aggregating", "completed", "cancelled"}
    assert set(get_args(ProgressStage)) == expected
    assert len(get_args(ProgressStage)) == 5
