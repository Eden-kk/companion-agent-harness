"""NativeDuplexEouSource — Protocol + concrete implementations for MiniCPM-o native_duplex EOU signal (v0.1j Task 8).

Spec Part 9: `native_duplex` is the final-product primary EOU producer.
When available it short-circuits SmartTurn v3 + semantic_eou (which become
fallback adapters). VAD silence gate always runs in parallel as safety net.

API used: `MiniCPMODuplex.streaming_generate()` returns a dict with `is_listen`
(bool).  When `is_listen=False` the model decided to speak — this is the native
EOU signal.  `MiniCPMStreamingModel._last_is_listen` is updated after each
`streaming_generate` call in `infer_stream` and is the state this source reads.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from companion_harness.schemas import TurnSignal

if TYPE_CHECKING:
    from companion_harness.foreground_model_minicpm import MiniCPMStreamingModel

__all__ = ["NativeDuplexEouSource", "_NullNativeDuplexEouSource", "MiniCPMNativeDuplexEouSource"]


@runtime_checkable
class NativeDuplexEouSource(Protocol):
    """Injected interface for the MiniCPM-o native_duplex EOU signal.

    Returns a TurnSignal when the model's internal is_listen gate fires
    (p_done high enough to trigger EOU), or None when the model has not
    crossed the threshold or is unavailable.
    """

    def get_eou_signal(self) -> TurnSignal | None: ...


class _NullNativeDuplexEouSource:
    """No-op fallback used in tests and stub-mode runs.

    Always returns None, routing every EOU decision to the SmartTurn v3 /
    VAD fallback path.
    """

    def get_eou_signal(self) -> TurnSignal | None:
        return None


class MiniCPMNativeDuplexEouSource:
    """NativeDuplexEouSource backed by MiniCPMStreamingModel.

    Reads `model._last_is_listen` — the result of the most recent
    `streaming_generate` call made by `infer_stream`.  When the model
    decided to speak (`is_listen=False`), returns a TurnSignal with
    `p_done=1.0` and detector="native_duplex".  When the model is still
    listening, returns None (routes to SmartTurn/VAD fallback).

    Thread-safety: `_last_is_listen` is a plain bool written from the
    infer_stream async task and read from the orchestrator loop; both run
    in the same asyncio event loop so no locking is required.
    """

    def __init__(self, model: "MiniCPMStreamingModel") -> None:
        self._model = model

    def get_eou_signal(self) -> TurnSignal | None:
        if self._model._last_is_listen:
            return None
        return TurnSignal(
            detector="native_duplex",
            p_done=1.0,
            p_continue=0.0,
            p_backchannel=0.0,
            confidence=0.95,
            evidence_event_ids=[str(uuid.uuid4())],
        )
