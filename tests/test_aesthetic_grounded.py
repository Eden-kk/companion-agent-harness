"""Grounded-content contract tests: no fabricated memory, no absent sensory channel (v0.1g Task 11).

Contract: proposals that invent autobiographical fiction (RUBRIC_FABRICATED_MEMORY) or
claim sensory channels the system does not have (RUBRIC_ABSENT_SENSORY_CHANNEL) are flagged;
proposals grounded in observable audio/video are not.
Success criterion: pytest tests/test_aesthetic_grounded.py -v → all passed.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# RUBRIC_FABRICATED_MEMORY
# ---------------------------------------------------------------------------

def test_fabricated_memory_childhood_reference() -> None:
    p = _proposal("I remember my childhood was full of this.")
    assert "RUBRIC_FABRICATED_MEMORY" in _rubric.check(p.content, {})


def test_fabricated_memory_used_to() -> None:
    p = _proposal("We used to sit here for hours.")
    assert "RUBRIC_FABRICATED_MEMORY" in _rubric.check(p.content, {})


def test_grounded_present_observation_no_fabricated_memory() -> None:
    p = _proposal("The light just shifted.")
    assert "RUBRIC_FABRICATED_MEMORY" not in _rubric.check(p.content, {})


def test_grounded_audio_observation_no_fabricated_memory() -> None:
    p = _proposal("That hum softened.")
    assert "RUBRIC_FABRICATED_MEMORY" not in _rubric.check(p.content, {})


# ---------------------------------------------------------------------------
# RUBRIC_ABSENT_SENSORY_CHANNEL
# ---------------------------------------------------------------------------

def test_absent_sensory_taste() -> None:
    p = _proposal("I taste something sweet.")
    assert "RUBRIC_ABSENT_SENSORY_CHANNEL" in _rubric.check(p.content, {})


def test_absent_sensory_scent() -> None:
    p = _proposal("The scent of pine drifts in.")
    assert "RUBRIC_ABSENT_SENSORY_CHANNEL" in _rubric.check(p.content, {})


def test_grounded_audio_no_absent_sensory() -> None:
    p = _proposal("That sizzle just softened.")
    assert "RUBRIC_ABSENT_SENSORY_CHANNEL" not in _rubric.check(p.content, {})


def test_grounded_visual_no_absent_sensory() -> None:
    p = _proposal("The shadow lengthened across the wall.")
    assert "RUBRIC_ABSENT_SENSORY_CHANNEL" not in _rubric.check(p.content, {})
