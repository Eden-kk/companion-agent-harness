"""Replay harness — Tier A (end-to-end behavioral) and Tier B (policy-layer exact).

See docs/architecture-v0.1.md §Part 2 invariants #5-#6 and §Part 6 Stage 0 for
the two replay tiers. Tier B replay is bit-identical; Tier A uses the behavioral
tuple (same_action_class + same_timing_bucket(+/-200ms) + same_interaction_intent
+ same_safety_class).
"""

from __future__ import annotations

import json
from dataclasses import astuple
from pathlib import Path

from companion_harness.reason_codes import ReasonCode
from companion_harness.schemas import SpeakDecision


def run_tier_b_replay(event_log_path: Path) -> list[SpeakDecision]:
    """Read a JSONL event log and return SpeakDecisions from policy_decision events.

    Each policy_decision event must carry payload_inline with action_type and
    primary_reason_code.  Fields not stored inline are set to their zero/empty
    defaults — sufficient for Tier-B comparison of the decision tuple.
    """
    decisions: list[SpeakDecision] = []
    with event_log_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("event_type") != "policy_decision":
                continue
            inline = obj.get("payload_inline") or {}
            decisions.append(SpeakDecision(
                action_type=inline["action_type"],
                primary_reason_code=ReasonCode(inline["primary_reason_code"]),
                supporting_reason_codes=[ReasonCode(rc) for rc in inline.get("supporting_reason_codes", [])],
                redacted_explanation=None,
                caused_by=obj.get("caused_by", []),
                budget_bucket=inline.get("budget_bucket"),
                allowed_prosody_tags=[],
                max_duration_ms=None,
                response_content_source=inline.get("response_content_source", "no_synthesis"),
            ))
    return decisions


def assert_bit_identical(
    original: list[SpeakDecision],
    replayed: list[SpeakDecision],
) -> None:
    """Assert two SpeakDecision sequences are bit-identical via astuple() comparison."""
    assert len(original) == len(replayed), (
        f"length mismatch: original={len(original)}, replayed={len(replayed)}"
    )
    for i, (a, b) in enumerate(zip(original, replayed)):
        assert astuple(a) == astuple(b), (
            f"decision[{i}] mismatch:\n  original={astuple(a)!r}\n  replayed={astuple(b)!r}"
        )
