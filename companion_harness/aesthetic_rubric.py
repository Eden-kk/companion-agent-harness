"""AestheticRubric — Protocol + _NullAestheticRubric stub (v0.1g Task 3).

See docs/roadmap-v0.1g-draft.md §Wave 2 Task 3 and docs/architecture-v0.1.md
Part 6 Stage 6 (spec lines 624-658) for the eight-check rubric definition.

Anchor 4 (roadmap): rubric enforcement lives inside SpeakPolicy.decide() — the
Protocol is injected there (Task 5). _NullAestheticRubric is the stub for Tasks
3-5; the concrete regex/lexicon implementation ships in Task 6.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from companion_harness.schemas import RubricViolation

__all__ = ["AestheticRubric", "_NullAestheticRubric"]


@runtime_checkable
class AestheticRubric(Protocol):
    def check(self, proposal_text: str, context: dict) -> list[RubricViolation]:
        """Return list of violated rubric IDs. Empty list = passes all 8 checks."""
        ...


class _NullAestheticRubric:
    """Stub that passes every proposal — no violations returned.

    Concrete regex/lexicon implementation ships in v0.1g Task 6.
    """

    def check(self, proposal_text: str, context: dict) -> list[RubricViolation]:
        return []
