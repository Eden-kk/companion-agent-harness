"""Concrete RegexAestheticRubric tests (v0.1g Task 6).

Success criterion: pytest tests/test_aesthetic_rubric_concrete.py -v → 17+ passed.
  - 8 violation tests (positive + negative, one per RubricViolation ID)
  - 1 clean-proposal test (passes all 8 checks)
"""

from __future__ import annotations

import pytest

from companion_harness.aesthetic_rubric import AestheticRubric, RegexAestheticRubric


@pytest.fixture()
def rubric() -> RegexAestheticRubric:
    return RegexAestheticRubric()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

def test_regex_rubric_satisfies_protocol(rubric: RegexAestheticRubric) -> None:
    assert isinstance(rubric, AestheticRubric)


# ---------------------------------------------------------------------------
# RUBRIC_TOO_LONG — spec line 624 (<= 8 words)
# ---------------------------------------------------------------------------

def test_too_long_triggers(rubric: RegexAestheticRubric) -> None:
    # 9 words — over the 8-word limit
    text = "the light on the water just changed quite beautifully"
    violations = rubric.check(text, {})
    assert "RUBRIC_TOO_LONG" in violations


def test_too_long_not_triggered_at_limit(rubric: RegexAestheticRubric) -> None:
    # exactly 7 words (safely under limit)
    text = "the light on the water changed"
    violations = rubric.check(text, {})
    assert "RUBRIC_TOO_LONG" not in violations


# ---------------------------------------------------------------------------
# RUBRIC_UNGROUNDED — spec line 629
# ---------------------------------------------------------------------------

def test_ungrounded_triggers(rubric: RegexAestheticRubric) -> None:
    text = "I think you are feeling reflective right now"
    violations = rubric.check(text, {})
    assert "RUBRIC_UNGROUNDED" in violations


def test_ungrounded_not_triggered_for_grounded(rubric: RegexAestheticRubric) -> None:
    text = "The steam just thickened."
    violations = rubric.check(text, {})
    assert "RUBRIC_UNGROUNDED" not in violations


# ---------------------------------------------------------------------------
# RUBRIC_POSSESSIVE — spec line 631
# ---------------------------------------------------------------------------

def test_possessive_triggers(rubric: RegexAestheticRubric) -> None:
    text = "Look what we have here."
    violations = rubric.check(text, {})
    assert "RUBRIC_POSSESSIVE" in violations


def test_possessive_not_triggered_for_neutral(rubric: RegexAestheticRubric) -> None:
    text = "Look at that."
    violations = rubric.check(text, {})
    assert "RUBRIC_POSSESSIVE" not in violations


# ---------------------------------------------------------------------------
# RUBRIC_DIAGNOSTIC — spec line 632
# ---------------------------------------------------------------------------

def test_diagnostic_triggers(rubric: RegexAestheticRubric) -> None:
    text = "You seem tired and stressed today."
    violations = rubric.check(text, {})
    assert "RUBRIC_DIAGNOSTIC" in violations


def test_diagnostic_not_triggered_for_observation(rubric: RegexAestheticRubric) -> None:
    text = "That sizzle softened."
    violations = rubric.check(text, {})
    assert "RUBRIC_DIAGNOSTIC" not in violations


# ---------------------------------------------------------------------------
# RUBRIC_FLATTERING — spec line 633
# ---------------------------------------------------------------------------

def test_flattering_triggers(rubric: RegexAestheticRubric) -> None:
    text = "Amazing! You're so talented."
    violations = rubric.check(text, {})
    assert "RUBRIC_FLATTERING" in violations


def test_flattering_not_triggered_for_neutral(rubric: RegexAestheticRubric) -> None:
    text = "The light shifted."
    violations = rubric.check(text, {})
    assert "RUBRIC_FLATTERING" not in violations


# ---------------------------------------------------------------------------
# RUBRIC_FABRICATED_MEMORY — spec line 629 corollary
# ---------------------------------------------------------------------------

def test_fabricated_memory_triggers(rubric: RegexAestheticRubric) -> None:
    text = "I remember my grandmother used to do this."
    violations = rubric.check(text, {})
    assert "RUBRIC_FABRICATED_MEMORY" in violations


def test_fabricated_memory_not_triggered_for_present(rubric: RegexAestheticRubric) -> None:
    text = "Oh — the light on the water changed."
    violations = rubric.check(text, {})
    assert "RUBRIC_FABRICATED_MEMORY" not in violations


# ---------------------------------------------------------------------------
# RUBRIC_ABSENT_SENSORY_CHANNEL — spec line 630 corollary
# ---------------------------------------------------------------------------

def test_absent_sensory_channel_triggers_smell(rubric: RegexAestheticRubric) -> None:
    text = "I smell something burning."
    violations = rubric.check(text, {})
    assert "RUBRIC_ABSENT_SENSORY_CHANNEL" in violations


def test_absent_sensory_channel_not_triggered_for_audio(rubric: RegexAestheticRubric) -> None:
    text = "That sizzle just softened."
    violations = rubric.check(text, {})
    assert "RUBRIC_ABSENT_SENSORY_CHANNEL" not in violations


# ---------------------------------------------------------------------------
# RUBRIC_IDENTITY_ONLY — spec line 639 (references texture/feel/change, not identity)
# ---------------------------------------------------------------------------

def test_identity_only_triggers(rubric: RegexAestheticRubric) -> None:
    text = "I see a tree."
    violations = rubric.check(text, {})
    assert "RUBRIC_IDENTITY_ONLY" in violations


def test_identity_only_not_triggered_for_texture(rubric: RegexAestheticRubric) -> None:
    text = "The bark caught the last of the light."
    violations = rubric.check(text, {})
    assert "RUBRIC_IDENTITY_ONLY" not in violations


# ---------------------------------------------------------------------------
# Clean proposal — passes all 8 checks
# ---------------------------------------------------------------------------

def test_rubric_pass_passes_all_8_checks(rubric: RegexAestheticRubric) -> None:
    # Short, grounded, non-possessive, non-diagnostic, non-flattering,
    # no fabricated memory, no absent sensory channel, not identity-only.
    text = "The light on the water changed."
    violations = rubric.check(text, {"available_sensors": ["audio", "video"]})
    assert violations == [], f"expected no violations, got {violations}"
