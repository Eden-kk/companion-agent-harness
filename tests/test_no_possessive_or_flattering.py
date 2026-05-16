"""Combined possessive and flattering rubric contract tests (v0.1g Task 13).

Contract: proposals using possessive language (RUBRIC_POSSESSIVE) or user-flattering
language (RUBRIC_FLATTERING) are flagged; neutral, world-focused proposals are not.
Success criterion: pytest tests/test_no_possessive_or_flattering.py -v → all passed.
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
# RUBRIC_POSSESSIVE — positive triggers
# ---------------------------------------------------------------------------

def test_possessive_our_space() -> None:
    p = _proposal("This is our space.")
    assert "RUBRIC_POSSESSIVE" in _rubric.check(p.content, {})


def test_possessive_look_what_we_have() -> None:
    p = _proposal("Look what we have here.")
    assert "RUBRIC_POSSESSIVE" in _rubric.check(p.content, {})


def test_possessive_our_world() -> None:
    p = _proposal("Welcome to our world.")
    assert "RUBRIC_POSSESSIVE" in _rubric.check(p.content, {})


# ---------------------------------------------------------------------------
# RUBRIC_POSSESSIVE — negative cases
# ---------------------------------------------------------------------------

def test_neutral_look_at_that_no_possessive() -> None:
    p = _proposal("Look at that.")
    assert "RUBRIC_POSSESSIVE" not in _rubric.check(p.content, {})


def test_neutral_world_observation_no_possessive() -> None:
    p = _proposal("The horizon shifted.")
    assert "RUBRIC_POSSESSIVE" not in _rubric.check(p.content, {})


# ---------------------------------------------------------------------------
# RUBRIC_FLATTERING — positive triggers
# ---------------------------------------------------------------------------

def test_flattering_amazing() -> None:
    p = _proposal("Amazing work today.")
    assert "RUBRIC_FLATTERING" in _rubric.check(p.content, {})


def test_flattering_so_talented() -> None:
    p = _proposal("You're so talented.")
    assert "RUBRIC_FLATTERING" in _rubric.check(p.content, {})


def test_flattering_brilliant() -> None:
    p = _proposal("That was brilliant.")
    assert "RUBRIC_FLATTERING" in _rubric.check(p.content, {})


# ---------------------------------------------------------------------------
# RUBRIC_FLATTERING — negative cases
# ---------------------------------------------------------------------------

def test_neutral_observation_no_flattering() -> None:
    p = _proposal("The light on the water changed.")
    assert "RUBRIC_FLATTERING" not in _rubric.check(p.content, {})


def test_neutral_sound_observation_no_flattering() -> None:
    p = _proposal("That melody slowed.")
    assert "RUBRIC_FLATTERING" not in _rubric.check(p.content, {})


# ---------------------------------------------------------------------------
# Combined — a proposal can violate both
# ---------------------------------------------------------------------------

def test_possessive_and_flattering_both_fire() -> None:
    p = _proposal("Amazing what we have here.")
    violations = _rubric.check(p.content, {})
    assert "RUBRIC_POSSESSIVE" in violations
    assert "RUBRIC_FLATTERING" in violations
