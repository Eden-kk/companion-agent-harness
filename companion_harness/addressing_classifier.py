"""AddressingClassifier — 3-tier derivation of `user_addressed_agent`.

Replaces the mechanical PR #142 derivation (`user_addressed_agent =
(social_mode == "user_addressing_agent")`) with a deterministic three-tier
classifier driven by observable signals:

    addressing_confidence =
        explicit    if wake-word detected (agent name appears in transcript)
        background  if multi-speaker detected (speaker_count >= 2)
        implicit    otherwise

    user_addressed_agent =
        True            if explicit
        False           if background
        mode_default    if implicit  (mechanical fallback to social_mode)

"No invented heuristics" discipline (project-lead direction):
  - explicit: deterministic string match on the configured wake-word(s).
    Standard industry pattern; uses the observable transcript signal only.
  - background: speaker-count signal. Speaker diarization is not wired in
    v0.1f, so callers pass speaker_count=None; this tier currently never
    fires "background". Forward-compatible for when diarization arrives.
  - implicit: falls through to the mechanical social_mode-based default
    (the pre-PR-#143 behavior).

No regex on transcript content beyond the wake-word string match. No
question/imperative/interjection lexicons. Those would be invented
heuristics; they belong to a later phase (see issue #139).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

__all__ = [
    "AddressingConfidence",
    "AddressingSignal",
    "AddressingClassifier",
    "WakeWordAddressingClassifier",
    "derive_user_addressed_agent",
]


AddressingConfidence = Literal["explicit", "background", "implicit"]


@dataclass(frozen=True)
class AddressingSignal:
    """Output of the addressing classifier — which tier fired + evidence string.

    `evidence` is a short, machine-readable string suitable for an event log
    payload: e.g. `"wake_word_match:companion"`, `"multi_speaker:2"`,
    `"implicit_fallback"`.
    """

    confidence: AddressingConfidence
    evidence: str


@runtime_checkable
class AddressingClassifier(Protocol):
    """Decides addressing tier from (transcript, speaker_count, social_mode).

    Returns an `AddressingSignal` capturing the tier and the evidence; the
    caller converts to `user_addressed_agent: bool` via
    `derive_user_addressed_agent`.

    Determinism (invariant #5): same inputs MUST produce the same output.
    """

    def __call__(
        self,
        transcript: str,
        speaker_count: int | None,
        social_mode: str,
    ) -> AddressingSignal: ...


def _tokenize(transcript: str) -> list[str]:
    """Lowercase + strip common terminal punctuation; return whole-word tokens.

    Replaces `, . ! ?` with spaces so `"hey companion!"` tokenizes as
    `["hey", "companion"]`. Does NOT match substrings: `"agentic"` will not
    match the agent name `"agent"` (word-boundary discipline).
    """
    normalized = transcript.lower().strip()
    for ch in (",", ".", "!", "?", ";", ":"):
        normalized = normalized.replace(ch, " ")
    return normalized.split()


@dataclass(frozen=True)
class WakeWordAddressingClassifier:
    """Deterministic 3-tier classifier.

    Tier logic:
      - explicit:    any agent name appears as a whole word in the transcript
                     (case-insensitive; common terminal punctuation stripped
                     so `"hey companion!"` matches `"companion"`).
      - background:  `speaker_count is not None and speaker_count >= 2`.
      - implicit:    otherwise (caller falls back to mode_default).

    Precedence: explicit > background > implicit. Wake-word wins when both
    a wake-word AND multi-speaker are present — the user explicitly named
    the agent, so address them regardless of who else is in the room.

    `agent_names` is configurable but defaults to a small fixed set so the
    classifier is usable without configuration. NO regex; NO content
    heuristics beyond the configured wake-word string.
    """

    agent_names: tuple[str, ...] = ("companion", "agent", "assistant")

    def __call__(
        self,
        transcript: str,
        speaker_count: int | None,
        social_mode: str,
    ) -> AddressingSignal:
        tokens = _tokenize(transcript)
        token_set = set(tokens)
        for name in self.agent_names:
            if name in token_set:
                return AddressingSignal(
                    confidence="explicit",
                    evidence=f"wake_word_match:{name}",
                )
        if speaker_count is not None and speaker_count >= 2:
            return AddressingSignal(
                confidence="background",
                evidence=f"multi_speaker:{speaker_count}",
            )
        return AddressingSignal(confidence="implicit", evidence="implicit_fallback")


def derive_user_addressed_agent(
    signal: AddressingSignal,
    social_mode: str,
) -> bool:
    """Convert `AddressingSignal` + `social_mode` into the `PolicyInputs` bool.

    explicit   -> True   (wake-word fired; user clearly addressing agent)
    background -> False  (multi-speaker detected; defer)
    implicit   -> mode-based mechanical fallback (pre-classifier behavior).
    """
    if signal.confidence == "explicit":
        return True
    if signal.confidence == "background":
        return False
    return social_mode == "user_addressing_agent"
