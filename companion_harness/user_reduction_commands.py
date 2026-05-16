"""User reduction command detection and budget mutation (v0.1g Task 9 / Wave 5).

Detects "less proactive" / "quiet mode" phrases in ASR transcripts and
returns the mutation to apply to PolicyInputs.proactivity_budget_remaining.

Per OQ-6: v0.1f locked the proactivity_budget_remaining alphabet as the
empty set, so `less_proactive` halves only `aesthetic_reaction` and
`quiet_mode` zeros only `aesthetic_reaction` (rather than a full alphabet).

Detection is regex/lexicon per OQ-2 (no learned classifier at v0.1g).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal

CommandType = Literal["less_proactive", "quiet_mode"]

_LESS_PROACTIVE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bless\s+proactive\b",
        r"\bbe\s+less\s+proactive\b",
        r"\bmore\s+quiet\b",
        r"\btone\s+it\s+down\b",
        r"\bchill\s+out\b",
    ]
)

_QUIET_MODE_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in [
        r"\bquiet\s+mode\b",
        r"\bgo\s+quiet\b",
        r"\bstop\s+talking\b",
        r"\bshh+\b",
        r"\bsilent\s+mode\b",
    ]
)

_QUIET_MODE_DURATION_MS = 2 * 60 * 60 * 1000  # 2 hours in ms


@dataclass(frozen=True)
class UserReductionCommandPayload:
    """Inline payload for user_reduction_command_applied events."""
    command_type: CommandType
    applied_at_ms: int


def detect_user_reduction_command(transcript: str) -> CommandType | None:
    """Return the command type if the transcript matches a reduction phrase.

    `quiet_mode` is checked first — it is the stronger signal (zeroing) and
    some phrases are supersets of `less_proactive` variants.
    Returns None when no reduction phrase is detected.
    """
    for pattern in _QUIET_MODE_PATTERNS:
        if pattern.search(transcript):
            return "quiet_mode"
    for pattern in _LESS_PROACTIVE_PATTERNS:
        if pattern.search(transcript):
            return "less_proactive"
    return None


def apply_user_reduction_command(
    command_type: CommandType,
    proactivity_budget_remaining: dict[str, int],
) -> dict[str, int]:
    """Return a new budget dict with the mutation applied.

    Per OQ-6 (alphabet = empty set), only `aesthetic_reaction` is mutated:
      less_proactive — halve aesthetic_reaction (floor 0).
      quiet_mode     — zero aesthetic_reaction (and sets quiet_mode_active
                       semantics; caller is responsible for the flag).

    Returns a new dict; does not mutate the input.
    """
    result = dict(proactivity_budget_remaining)
    current = result.get("aesthetic_reaction", 0)
    if command_type == "less_proactive":
        result["aesthetic_reaction"] = current // 2
    else:
        result["aesthetic_reaction"] = 0
    return result


def make_user_reduction_payload(command_type: CommandType) -> UserReductionCommandPayload:
    return UserReductionCommandPayload(
        command_type=command_type,
        applied_at_ms=int(time.monotonic() * 1000),
    )
