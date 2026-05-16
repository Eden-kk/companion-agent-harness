"""AestheticRubric — Protocol + concrete regex/lexicon impl (v0.1g Tasks 3 + 6).

See docs/roadmap-v0.1g-draft.md §Wave 2 Task 3, §Wave 3 Task 6, and
docs/architecture-v0.1.md Part 6 Stage 6 (spec lines 624-658) for the
eight-check rubric definition.

Anchor 4 (roadmap): rubric enforcement lives inside SpeakPolicy.decide() — the
Protocol is injected there (Task 5). _NullAestheticRubric is the pass-all stub;
RegexAestheticRubric is the concrete OQ-2-aligned (regex/lexicon) implementation.
"""

from __future__ import annotations

import re
from typing import Protocol, runtime_checkable

from companion_harness.schemas import RubricViolation

__all__ = ["AestheticRubric", "_NullAestheticRubric", "RegexAestheticRubric"]


@runtime_checkable
class AestheticRubric(Protocol):
    def check(self, proposal_text: str, context: dict) -> list[RubricViolation]:
        """Return list of violated rubric IDs. Empty list = passes all 8 checks."""
        ...


class _NullAestheticRubric:
    """Stub that passes every proposal — no violations returned."""

    def check(self, proposal_text: str, context: dict) -> list[RubricViolation]:
        return []


# ---------------------------------------------------------------------------
# Lexicons used by RegexAestheticRubric
# ---------------------------------------------------------------------------

# spec line 629: no fabricated personal memory (no "my grandmother used to...",
# no autobiographical fiction the system does not actually have)
_FABRICATED_MEMORY_PATTERNS = re.compile(
    r"\b(i remember|my (grandmother|grandfather|mother|father|parent|childhood|past|history)|"
    r"we used to|i used to|back when i|when i was (young|a child|little|growing up)|"
    r"i once|i've always|i grew up)\b",
    re.IGNORECASE,
)

# spec line 630: no claimed sensory channel the system does not have
# (no smell, no taste, no touch unless wired)
_ABSENT_SENSORY_CHANNEL_PATTERNS = re.compile(
    r"\b(i (smell|smells?|taste|tastes?|feel|feels?|touch|touches?)|"
    r"(smells?|tastes?|feels?) like|the (scent|aroma|flavor|texture|warmth|coldness|roughness|smoothness))\b",
    re.IGNORECASE,
)

# spec line 631: non-possessive ("look at that" not "look what we have")
_POSSESSIVE_PATTERNS = re.compile(
    r"\b(our|we have|look what we|what we('ve| have)|our (space|place|home|room|view|world)|"
    r"my (friend|companion|person|human|user))\b",
    re.IGNORECASE,
)

# spec line 632: non-diagnostic (doesn't claim to know user's inner state)
_DIAGNOSTIC_PATTERNS = re.compile(
    r"\b(you('re| are) (feeling|stressed|anxious|sad|happy|depressed|tired|exhausted|overwhelmed|lonely|upset)|"
    r"(you seem|you look|you appear|you sound) (stressed|anxious|sad|tired|depressed|lonely|upset|worried)|"
    r"i can tell (you|that you)|your (anxiety|depression|stress|mood|emotions?|inner state|mental state)|"
    r"you('re| are) (struggling|suffering|grieving|mourning))\b",
    re.IGNORECASE,
)

# spec line 633: non-flattering of user
_FLATTERING_PATTERNS = re.compile(
    r"\b(amazing|incredible|wonderful|fantastic|brilliant|genius|so (smart|talented|gifted|creative|beautiful|handsome)|"
    r"you('re| are) (so|really|truly|absolutely) (great|awesome|special|unique|exceptional|extraordinary|talented)|"
    r"you did (it|that) (perfectly|brilliantly|amazingly|wonderfully))\b",
    re.IGNORECASE,
)

# spec line 639: references texture/feel/change, not just object identity
# Identity-only: "I see a sunset." / "I see a tree. It is green."
_IDENTITY_ONLY_PATTERNS = re.compile(
    r"^(i see (a|an|the) \w+\.?|there (is|are) (a|an|the) \w+\.?|"
    r"it is (a|an|the) \w+\.?|that('s| is) (a|an|the) \w+\.?)$",
    re.IGNORECASE,
)

# spec line 629: ungrounded — speculation without sensor grounding
_UNGROUNDED_PATTERNS = re.compile(
    r"\b(i imagine|i think (you|this|that)|perhaps you|maybe you|"
    r"probably (you|your)|you might (be|feel|want)|you must (be|feel)|"
    r"i suppose|i assume|i bet (you|that))\b",
    re.IGNORECASE,
)


class RegexAestheticRubric:
    """Concrete regex/lexicon rubric — OQ-2 aligned (no learned classifier).

    Implements all 8 RubricViolation IDs per spec lines 624-658 and
    docs/roadmap-v0.1g-draft.md §Wave 3 Task 6 + Anchor 1.

    context keys consumed (all optional):
      "available_sensors": list[str] — sensors active in this session.
        Defaults to ["audio"] if absent (conservative: no vision, no haptics).
    """

    # spec line 624: short (<= 8 words by default)
    MAX_WORDS: int = 8

    def check(self, proposal_text: str, context: dict) -> list[RubricViolation]:
        violations: list[RubricViolation] = []
        text = proposal_text.strip()

        # RUBRIC_TOO_LONG — spec line 624
        if len(text.split()) > self.MAX_WORDS:
            violations.append("RUBRIC_TOO_LONG")

        # RUBRIC_UNGROUNDED — spec line 629
        if _UNGROUNDED_PATTERNS.search(text):
            violations.append("RUBRIC_UNGROUNDED")

        # RUBRIC_POSSESSIVE — spec line 631
        if _POSSESSIVE_PATTERNS.search(text):
            violations.append("RUBRIC_POSSESSIVE")

        # RUBRIC_DIAGNOSTIC — spec line 632
        if _DIAGNOSTIC_PATTERNS.search(text):
            violations.append("RUBRIC_DIAGNOSTIC")

        # RUBRIC_FLATTERING — spec line 633
        if _FLATTERING_PATTERNS.search(text):
            violations.append("RUBRIC_FLATTERING")

        # RUBRIC_FABRICATED_MEMORY — spec line 629 corollary
        if _FABRICATED_MEMORY_PATTERNS.search(text):
            violations.append("RUBRIC_FABRICATED_MEMORY")

        # RUBRIC_ABSENT_SENSORY_CHANNEL — spec line 630 corollary
        # smell/taste/touch are never wired by default; flag unconditionally
        if _ABSENT_SENSORY_CHANNEL_PATTERNS.search(text):
            violations.append("RUBRIC_ABSENT_SENSORY_CHANNEL")

        # RUBRIC_IDENTITY_ONLY — spec line 639
        if _IDENTITY_ONLY_PATTERNS.match(text):
            violations.append("RUBRIC_IDENTITY_ONLY")

        return violations
