"""AestheticRubric Protocol + _NullAestheticRubric stub tests (v0.1g Task 3).

Success criteria:
  - AestheticRubric is runtime_checkable; _NullAestheticRubric satisfies it.
  - _NullAestheticRubric.check() returns [] for all 8 rubric-violation IDs.
  - RubricViolation Literal has exactly 8 members (alphabet guard).
"""

from __future__ import annotations

from typing import get_args

from companion_harness.aesthetic_rubric import AestheticRubric, _NullAestheticRubric
from companion_harness.schemas import RubricViolation

_EXPECTED_RUBRIC_IDS = {
    "RUBRIC_TOO_LONG",
    "RUBRIC_UNGROUNDED",
    "RUBRIC_POSSESSIVE",
    "RUBRIC_DIAGNOSTIC",
    "RUBRIC_FLATTERING",
    "RUBRIC_FABRICATED_MEMORY",
    "RUBRIC_ABSENT_SENSORY_CHANNEL",
    "RUBRIC_IDENTITY_ONLY",
}


def test_aesthetic_rubric_protocol_runtime_checkable() -> None:
    rubric = _NullAestheticRubric()
    assert isinstance(rubric, AestheticRubric)


def test_null_aesthetic_rubric_returns_empty_list() -> None:
    rubric = _NullAestheticRubric()
    for violation_id in _EXPECTED_RUBRIC_IDS:
        result = rubric.check(f"proposal triggering {violation_id}", {"rubric_id": violation_id})
        assert result == [], f"expected [] for {violation_id}, got {result}"


def test_rubric_violation_ids_alphabet() -> None:
    members = set(get_args(RubricViolation))
    assert members == _EXPECTED_RUBRIC_IDS
