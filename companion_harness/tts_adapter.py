"""TtsAdapter — audio synthesis interface, mapped to the spec's ProsodyController.

See docs/architecture-v0.1.md §Part 1 (ProsodyController: expressive prosody tag
rendering) and §Part 3 (ProsodyController adapter slot).

## Mapping: TtsAdapter IS the ProsodyController

The spec names `ProsodyController` as the adapter responsible for synthesis and
expressive prosody tag rendering.  `TtsAdapter` is this repo's concrete Protocol
for that adapter slot.  The name reflects v0.1 scope (bare synthesis; prosody
tags passed through from SpeakDecision.allowed_prosody_tags).  A future PR can
extend this for full expressive rendering once the production backend lands.

## Invariant boundaries (non-negotiable)

Synthesis is invoked ONLY downstream of a policy-approved SpeakDecision
(invariants #2, #4).  TtsAdapter has NO path to produce audio independently.
Callers must hold a SpeakDecision before calling synthesize().  The contract test
asserts that every synthesis event traces causally to a SpeakDecision event.

## Production backend

The real backend (MiniCPM-o as_duplex audio or CosyVoice2) is a follow-on task.
`SilentTtsAdapter` is the minimal deterministic stub used for contract tests only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

__all__ = ["TtsAdapter", "SilentTtsAdapter"]


@runtime_checkable
class TtsAdapter(Protocol):
    """Audio synthesis interface — the ProsodyController adapter slot.

    synthesize() returns an AsyncIterator[bytes] so AudioOutputController.play()
    can stream chunks as they arrive.  Callers obtain a SpeakDecision first and
    pass its allowed_prosody_tags here; the adapter may use them for expressive
    rendering (future) or ignore them (stub).
    """

    def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]: ...


class SilentTtsAdapter:
    """Minimal deterministic TtsAdapter for contract tests.

    Produces `chunk_count` copies of `chunk_bytes` — no inference, no I/O.
    Deterministic: same inputs → same byte stream, same chunk count.
    The production backend (MiniCPM-o as_duplex audio or CosyVoice2) is a
    follow-on task and implements TtsAdapter without touching this class.
    """

    def __init__(
        self,
        chunk_bytes: bytes = b"\x00" * 160,  # 160 bytes ≈ 10 ms of 16 kHz 8-bit PCM
        chunk_count: int = 3,
    ) -> None:
        self._chunk = chunk_bytes
        self._count = chunk_count

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:  # type: ignore[override]
        for _ in range(self._count):
            yield self._chunk
