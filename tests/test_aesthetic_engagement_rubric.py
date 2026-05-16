"""Exhaustive positive-trigger test for all 8 RubricViolation IDs (v0.1g Task 10).

Contract: for each of the 8 RubricViolation IDs, there exists a ThinkerProposal
whose content triggers exactly that violation when checked by RegexAestheticRubric.
Success criterion: pytest tests/test_aesthetic_engagement_rubric.py -v → 8 passed.
"""

from __future__ import annotations

import pytest

from companion_harness.aesthetic_rubric import RegexAestheticRubric
from companion_harness.schemas import ThinkerProposal


def _proposal(content: str) -> ThinkerProposal:
    return ThinkerProposal(
        proposal_type="aesthetic_reaction",
        content=content,
        trigger="scene_change",
        confidence=0.8,
        novelty=0.7,
        interruption_cost=0.1,
        max_utterance_ms=3000,
        cooldown_consumed="aesthetic_reaction",
        caused_by=["sig-1"],
    )


_rubric = RegexAestheticRubric()


@pytest.mark.parametrize("violation_id,content", [
    (
        "RUBRIC_TOO_LONG",
        "the light on the water just changed quite beautifully today",
    ),
    (
        "RUBRIC_UNGROUNDED",
        "I think you are feeling something right now.",
    ),
    (
        "RUBRIC_POSSESSIVE",
        "Look what we have here.",
    ),
    (
        "RUBRIC_DIAGNOSTIC",
        "You seem tired and stressed.",
    ),
    (
        "RUBRIC_FLATTERING",
        "Amazing — you are so talented.",
    ),
    (
        "RUBRIC_FABRICATED_MEMORY",
        "I remember my grandmother loved this.",
    ),
    (
        "RUBRIC_ABSENT_SENSORY_CHANNEL",
        "I smell something burning.",
    ),
    (
        "RUBRIC_IDENTITY_ONLY",
        "I see a tree.",
    ),
])
def test_positive_trigger(violation_id: str, content: str) -> None:
    proposal = _proposal(content)
    violations = _rubric.check(proposal.content, {})
    assert violation_id in violations, (
        f"expected {violation_id!r} in violations for {content!r}, got {violations}"
    )
