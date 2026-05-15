"""v0.1f tool-progress schema primitives (Anchor 1 + Anchor 4).

Locks the `progress_stage` Literal alphabet and the `ToolProgressEvidence`
dataclass that `PolicyInputs.tool_progress_evidence` carries.  No emission
code in this module — `ToolProgressEmitter` lives in its own module (v0.1f
Task 4); this file ships ONLY the durable schema primitives that downstream
tasks depend on.

Per docs/roadmap-v0.1f-draft.md Anchor 1 the `progress_stage` Literal
alphabet is durable metadata: once events are written with these labels,
renaming costs a one-time pass over the event log AND invalidates any
replay run that consumes the labels.  No "unknown" / "other" catch-all —
invariant #9 demands every stage be a specific, narratable label.

Per Anchor 4, `ToolProgressEvidence` is the filler-budget state-machine
view consumed by `SpeakPolicy.decide()` via
`PolicyInputs.tool_progress_evidence`.  Replay determinism (invariant #5)
holds because `ToolProgressEmitter.evidence_at(tool_call_id, now_mono_ms)`
(Task 4) derives `ms_since_last_filler` from `timestamp_mono_ms` deltas
between `tool_progress_event`s read out of the event log, never from a
live wall-clock read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


ProgressStage = Literal[
    "started",       # tool_call_dispatched fired; work has begun
    "scanning",      # in-progress; bulk read / search-phase work
    "aggregating",   # in-progress; results being combined / summarized
    "completed",     # work finished; tool_call_completed about to fire
    "cancelled",     # work cancelled (typically via barge-in)
]


@dataclass(frozen=True)
class ToolProgressEvidence:
    """Filler-budget state-machine view surfaced to SpeakPolicy (Anchor 4).

    Fields:
      progress_stage          — the current stage label from ProgressStage.
      fillers_emitted_so_far  — count of tool_status utterances already
                                emitted for this tool call (per-call scope
                                per Anchor 4 / spec line 572).
      ms_since_last_filler    — monotonic-ms delta since the last filler;
                                derived from event-log timestamps, never
                                from a live wall-clock (invariant #5).
      silence_won_already     — True iff the silence-wins-after-first-filler
                                short-circuit has fired for this call
                                (spec line 575).  Once True, tool_status is
                                suppressed for the remainder of the call.

    Frozen so the value can be hashed / stored on a DecisionTrace input
    audit trail; SpeakPolicy.decide() reads these fields and does not
    mutate them.
    """

    progress_stage:         ProgressStage
    fillers_emitted_so_far: int
    ms_since_last_filler:   int
    silence_won_already:    bool
