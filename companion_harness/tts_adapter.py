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

## Concrete backends

`SilentTtsAdapter` (below) is the minimal deterministic stub used for fixture
contract tests — it produces fixed silent PCM chunks and has no I/O. The
first audible backend is `KokoroTtsAdapter` (`tts_kokoro.py`), pure-ONNX
Kokoro-82M. The spec-canonical CosyVoice2 (Part 9) and MiniCPM-o native
`as_duplex` audio (OQ-1 preference) remain in-scope swaps — see
`tts_kokoro.py`'s module docstring for the platform analysis that gated
the Path-C decision on the current b200 venv.
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

    **Cancellation contract.** ``synthesize()`` returns an async iterator. When the
    consumer's task is cancelled, Python propagates ``CancelledError`` into the
    generator at its current ``yield`` point. Implementations MUST NOT swallow
    ``CancelledError``. If an implementation holds non-Python state (e.g. an
    outstanding HTTP request, a GPU decode kernel, a CosyVoice2 stream handle),
    it MUST release it in a ``finally:`` block or ``__aexit__`` and then let
    ``CancelledError`` propagate.

    A separate ``aclose()`` is NOT required: ``CancelledError`` propagation through
    the generator is sufficient for the harness's barge-in path. The graceful
    path uses ``AudioOutputController.request_stop()``, which sets a stop-event
    consulted between chunk yields — this requires no cooperation from the
    adapter beyond yielding chunks of bounded size.
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

    async def synthesize(self, text: str, prosody_tags: list[str]) -> AsyncIterator[bytes]:
        for _ in range(self._count):
            yield self._chunk
