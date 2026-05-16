"""Diagnostic / clinical-language contract tests (v0.1g Task 12).

Contract: proposals that claim to know the user's affective or clinical state
(RUBRIC_DIAGNOSTIC) are flagged; proposals that observe the world without
making inner-state claims are not.
Success criterion: pytest tests/test_affective_hypothesis_not_surfaceable.py -v → all passed.
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
# Positive triggers — clinical / affective inner-state claims
# ---------------------------------------------------------------------------

def test_diagnostic_you_seem_anxious() -> None:
    p = _proposal("You seem anxious right now.")
    assert "RUBRIC_DIAGNOSTIC" in _rubric.check(p.content, {})


def test_diagnostic_you_are_feeling_depressed() -> None:
    p = _proposal("You're feeling depressed today.")
    assert "RUBRIC_DIAGNOSTIC" in _rubric.check(p.content, {})


def test_diagnostic_you_are_stressed() -> None:
    p = _proposal("You are stressed.")
    assert "RUBRIC_DIAGNOSTIC" in _rubric.check(p.content, {})


def test_diagnostic_you_seem_lonely() -> None:
    p = _proposal("You seem lonely.")
    assert "RUBRIC_DIAGNOSTIC" in _rubric.check(p.content, {})


def test_diagnostic_your_anxiety() -> None:
    p = _proposal("Your anxiety shows in your voice.")
    assert "RUBRIC_DIAGNOSTIC" in _rubric.check(p.content, {})


# ---------------------------------------------------------------------------
# Negative cases — world observations without inner-state claims
# ---------------------------------------------------------------------------

def test_world_observation_no_diagnostic() -> None:
    p = _proposal("The rain just started outside.")
    assert "RUBRIC_DIAGNOSTIC" not in _rubric.check(p.content, {})


def test_sound_observation_no_diagnostic() -> None:
    p = _proposal("That note hung in the air.")
    assert "RUBRIC_DIAGNOSTIC" not in _rubric.check(p.content, {})
