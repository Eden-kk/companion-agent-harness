"""LLM-driven provenance computer for SleepTimeAgent (issue #188).

Provides:
  ProvenanceComputer  — Protocol (any callable with .compute()).
  MiniCPMProvenanceComputer  — MiniCPM-o backed implementation.
  _NullProvenanceComputer    — Fallback stub (UNAVAILABLE: #188 — null path).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    pass

__all__ = [
    "ProvenanceComputer",
    "MiniCPMProvenanceComputer",
    "_NullProvenanceComputer",
]


class ProvenanceComputer(Protocol):
    """Compute (confidence, salience, user_visible_summary) for a memory candidate."""

    def compute(self, candidate: dict) -> tuple[float, float, str]: ...


_PROVENANCE_PROMPT = (
    "You are a memory provenance assistant. Given the following memory candidate, "
    "respond ONLY with three lines:\n"
    "confidence: <float 0.0-1.0>\n"
    "salience: <float 0.0-1.0>\n"
    "summary: <one short sentence describing the memory, max 80 chars>\n\n"
    "Memory candidate: {content}"
)

_CONFIDENCE_DEFAULT = 0.8
_SALIENCE_DEFAULT = 0.5


class MiniCPMProvenanceComputer:
    """MiniCPM-o backed implementation of ProvenanceComputer.

    Uses MiniCPMDuplexModel.chat() to derive confidence, salience, and
    user_visible_summary from the memory candidate's content field.
    Falls back to defaults on any parse or inference failure.
    """

    def __init__(self, minicpm_text_model: "object") -> None:
        self._model = minicpm_text_model

    def compute(self, candidate: dict) -> tuple[float, float, str]:
        content = candidate.get("content", {})
        prompt = _PROVENANCE_PROMPT.format(content=str(content))
        try:
            raw = self._model.chat(prompt, max_new_tokens=64)  # type: ignore[attr-defined]
        except Exception:
            return _CONFIDENCE_DEFAULT, _SALIENCE_DEFAULT, str(content)[:80]
        return _parse_provenance_response(raw, content)


def _parse_provenance_response(raw: str, content: object) -> tuple[float, float, str]:
    confidence = _CONFIDENCE_DEFAULT
    salience = _SALIENCE_DEFAULT
    summary = str(content)[:80]
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("confidence:"):
            try:
                confidence = float(line.split(":", 1)[1].strip())
                confidence = max(0.0, min(1.0, confidence))
            except ValueError:
                pass
        elif line.startswith("salience:"):
            try:
                salience = float(line.split(":", 1)[1].strip())
                salience = max(0.0, min(1.0, salience))
            except ValueError:
                pass
        elif line.startswith("summary:"):
            summary = line.split(":", 1)[1].strip()[:80]
    return confidence, salience, summary


class _NullProvenanceComputer:
    """Stub used when no LLM model is available.

    Returns static defaults. UNAVAILABLE: #188 — null path uses stub defaults.
    """

    def compute(self, candidate: dict) -> tuple[float, float, str]:
        content = candidate.get("content", {})
        return _CONFIDENCE_DEFAULT, _SALIENCE_DEFAULT, str(content)[:80]
