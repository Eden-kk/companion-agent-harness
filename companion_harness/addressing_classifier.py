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
        False           if implicit AND (transcript too short OR denylist hit)
        True            if implicit AND transcript substantive (≥3 tokens, not denylist)

Invariant #8 (silence wins ties): when signal is ambiguous (implicit tier),
the default is False. A transcript must have ≥ IMPLICIT_MIN_TOKENS tokens
AND must not match any phrase in WHISPER_HALLUCINATION_DENYLIST to flip True.

"No invented heuristics" discipline (project-lead direction):
  - explicit: deterministic string match on the configured wake-word(s).
    Standard industry pattern; uses the observable transcript signal only.
  - background: speaker-count signal. Speaker diarization is not wired in
    v0.1f, so callers pass speaker_count=None; this tier currently never
    fires "background". Forward-compatible for when diarization arrives.
  - implicit: defaults False; requires substantive transcript evidence.

No regex on transcript content beyond the wake-word string match. No
question/imperative/interjection lexicons. Those would be invented
heuristics; they belong to a later phase (see issue #139).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from companion_harness.foreground_model_minicpm import MiniCPMDuplexModel

__all__ = [
    "AddressingConfidence",
    "AddressingSignal",
    "AddressingClassifier",
    "MiniCPMAddressingClassifier",
    "MiniCPMAddressingClassifierImpl",
    "_NullMiniCPMAddressingClassifier",
    "WakeWordAddressingClassifier",
    "WHISPER_HALLUCINATION_DENYLIST",
    "IMPLICIT_MIN_TOKENS",
    "derive_user_addressed_agent",
]

# Minimum token count for an implicit-tier transcript to be considered
# substantive (invariant #8: silence wins ties).
IMPLICIT_MIN_TOKENS: int = 3

# Whisper-tiny hallucination phrases commonly produced from silence or noise.
# Normalised to lowercase; punctuation stripped to match _tokenize() output.
# When the full joined transcript matches one of these exactly, implicit tier
# returns False regardless of social_mode.
WHISPER_HALLUCINATION_DENYLIST: frozenset[str] = frozenset({
    "you",
    "the",
    ".",
    "bye",
    "bye bye",
    "thank you",
    "thanks",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "subscribe",
    "like and subscribe",
    "hmm",
    "uh",
    "um",
    "uh huh",
})


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


@runtime_checkable
class MiniCPMAddressingClassifier(Protocol):
    """MiniCPM-o derived addressing classifier — final-product primary producer.

    Returns `AddressingSignal` when inference is available, or `None` when the
    model is unavailable so the orchestrator falls back to the safety-net
    (`WakeWordAddressingClassifier`).
    """

    def __call__(
        self,
        transcript: str,
        speaker_count: int | None,
        social_mode: str,
    ) -> AddressingSignal | None: ...


_ADDRESSING_PROMPT = (
    "Transcript: '{transcript}'. "
    "Is the user addressing an AI assistant? "
    "Answer with only 'yes' or 'no'."
)


class MiniCPMAddressingClassifierImpl:
    """MiniCPM-o backed implementation of MiniCPMAddressingClassifier.

    Uses MiniCPMDuplexModel.chat() with a yes/no prompt to determine
    addressing intent from the transcript text.  Audio bytes are accepted
    for interface compatibility but are not used (text-only path).

    Returns AddressingSignal or None if the model response is unparseable.
    """

    def __init__(self, model: "MiniCPMDuplexModel") -> None:
        self._model = model

    def __call__(
        self,
        transcript: str,
        speaker_count: int | None,
        social_mode: str,
    ) -> AddressingSignal | None:
        if not transcript.strip():
            return None
        prompt = _ADDRESSING_PROMPT.format(transcript=transcript)
        try:
            raw = self._model.chat(prompt, max_new_tokens=4)
        except Exception:
            return None
        answer = raw.strip().lower()
        if answer.startswith("yes"):
            return AddressingSignal(confidence="explicit", evidence="minicpm_classifier:yes")
        if answer.startswith("no"):
            return AddressingSignal(confidence="implicit", evidence="minicpm_classifier:no")
        return None


class _NullMiniCPMAddressingClassifier:
    """Stub for `MiniCPMAddressingClassifier` when MiniCPM model is not loaded.

    Always returns `None` so the orchestrator falls back to
    `WakeWordAddressingClassifier` (safety-net).

    Emits a `signal_producer_fallback` event once at construction when a
    logger is provided, so replay knows MiniCPM addressing was unavailable
    for the entire session.
    """

    def __init__(self, logger: Any = None, session_id: str = "") -> None:
        if logger is not None:
            from companion_harness.schemas import Event  # local import avoids circularity
            now_ms = int(time.monotonic() * 1000)
            logger.log(Event(
                event_id=f"null-minicpm-addressing-{uuid.uuid4().hex[:8]}",
                session_id=session_id,
                schema_version="0.1",
                seq_no=0,
                event_type="signal_producer_fallback",
                timestamp_mono_ms=now_ms,
                timestamp_wall="",
                source="addressing_classifier",
                caused_by=["session_open"],
                payload_hash="",
                payload_ref=None,
                payload_kind="signal",
                subject_class="unknown",
                sensitivity="safe",
                retention_policy_id="signal_default_30d",
                payload_inline={
                    "primary_producer": "MiniCPMAddressingClassifier",
                    "fallback_producer": "WakeWordAddressingClassifier",
                    "reason": "minicpm_text_model not wired",
                },
            ))

    def __call__(
        self,
        transcript: str,
        speaker_count: int | None,
        social_mode: str,
    ) -> AddressingSignal | None:
        return None


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
    """Deterministic 3-tier safety-net classifier.

    Acts as the safety-net when the MiniCPM-derived primary is unavailable
    (see `_NullMiniCPMAddressingClassifier` and issue #157).

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
    transcript: str = "",
    current_speaker_id: str | None = None,
    last_anchored_speaker_id: str | None = None,
) -> bool:
    """Convert `AddressingSignal` + `social_mode` into the `PolicyInputs` bool.

    explicit   -> True   (wake-word fired; user clearly addressing agent)
    background -> False  (multi-speaker detected; defer)
    implicit   -> False  UNLESS transcript is substantive:
                         ≥ IMPLICIT_MIN_TOKENS tokens AND not in
                         WHISPER_HALLUCINATION_DENYLIST.
                         Invariant #8: silence wins ties.

    v0.1k speaker-continuity tie-breaker (Anchor 7): if the implicit/background
    tier would return False AND `current_speaker_id` matches `last_anchored_speaker_id`
    (the speaker captured at the most recent wake-word confirmation), return True.
    The tie-breaker is placed here (not in MiniCPMAddressingClassifier) because
    MiniCPM is gated on issue #157; this path is always active.

    `transcript` is optional for log-only call sites that don't need the
    implicit-tier gate; omitting it conservatively returns False for implicit.
    `current_speaker_id` and `last_anchored_speaker_id` are optional; when either
    is None the tie-breaker is skipped (backward-compatible default).
    """
    if signal.confidence == "explicit":
        return True
    if signal.confidence == "background":
        # Background tier returns False, but speaker-continuity can override.
        return _speaker_continuity_override(current_speaker_id, last_anchored_speaker_id)
    # Implicit tier: require positive evidence from the transcript.
    tokens = _tokenize(transcript)
    if len(tokens) < IMPLICIT_MIN_TOKENS:
        return _speaker_continuity_override(current_speaker_id, last_anchored_speaker_id)
    normalised = " ".join(tokens)
    if normalised in WHISPER_HALLUCINATION_DENYLIST:
        return _speaker_continuity_override(current_speaker_id, last_anchored_speaker_id)
    return True


def _speaker_continuity_override(
    current_speaker_id: str | None,
    last_anchored_speaker_id: str | None,
) -> bool:
    """Return True when the same speaker who triggered the last wake-word is still talking."""
    return (
        current_speaker_id is not None
        and last_anchored_speaker_id is not None
        and current_speaker_id == last_anchored_speaker_id
    )
