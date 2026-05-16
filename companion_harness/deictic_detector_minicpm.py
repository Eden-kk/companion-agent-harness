"""MiniCPMDeicticDetector — real DeicticModel implementation (closes #169, RFC #237).

Uses an already-loaded MiniCPMDuplexModel via `chat()` with a yes/no+confidence
prompt to determine whether the user's utterance refers to something visually
present (deictic reference: "this", "that", "look at X").  Mirrors the pattern
used by `MiniCPMAddressingClassifierImpl` (PR #209) and
`MiniCPMProvenanceComputer` (PR #235): no new model load, text-only prompt,
strict parser, graceful failure to `(False, 0.0)`.

Audio frame bytes are accepted for `DeicticModel` Protocol compatibility but
are not used (text-only path matches addressing classifier).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from companion_harness.foreground_model_minicpm import MiniCPMDuplexModel

__all__ = ["MiniCPMDeicticDetector"]


_DEICTIC_PROMPT = (
    "In the user's message: '{transcript}', does the user refer to something "
    "visually present (e.g. 'this', 'that', 'look at X')? "
    "Reply 'yes' or 'no' and a confidence 0-1."
)

_PARSE_RE = re.compile(r"(yes|no)[^0-9]*([01]?\.\d+|[01])", re.IGNORECASE)


class MiniCPMDeicticDetector:
    """MiniCPM-o backed DeicticModel.

    Calls `MiniCPMDuplexModel.chat()` with a yes/no+confidence prompt and
    parses the response.  Malformed responses, exceptions, and empty
    transcripts all yield `(False, 0.0)`.  A `no` answer always yields
    confidence 0.0.
    """

    def __init__(self, model: "MiniCPMDuplexModel") -> None:
        self._model = model

    def __call__(
        self, transcript: str, audio_buffer: bytes | None
    ) -> tuple[bool, float]:
        if not transcript or not transcript.strip():
            return (False, 0.0)
        prompt = _DEICTIC_PROMPT.format(transcript=transcript)
        try:
            raw = self._model.chat(prompt, max_new_tokens=16)
        except Exception:
            return (False, 0.0)
        match = _PARSE_RE.search(raw or "")
        if match is None:
            return (False, 0.0)
        answer = match.group(1).lower()
        try:
            confidence = float(match.group(2))
        except (TypeError, ValueError):
            return (False, 0.0)
        if answer == "yes":
            return (True, confidence)
        return (False, 0.0)
