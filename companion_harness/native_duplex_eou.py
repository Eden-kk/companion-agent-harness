"""NativeDuplexEouSource — Protocol for MiniCPM-o native_duplex EOU signal (v0.1j Task 8).

Spec Part 9: `native_duplex` is the final-product primary EOU producer.
When available it short-circuits SmartTurn v3 + semantic_eou (which become
fallback adapters). VAD silence gate always runs in parallel as safety net.

# UNAVAILABLE: #157 — libcudart blocker, native_duplex EOU unavailable
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from companion_harness.schemas import TurnSignal

__all__ = ["NativeDuplexEouSource", "_NullNativeDuplexEouSource"]


@runtime_checkable
class NativeDuplexEouSource(Protocol):
    """Injected interface for the MiniCPM-o native_duplex EOU signal.

    Returns a TurnSignal when the model's internal is_listen gate fires
    (p_done high enough to trigger EOU), or None when the model has not
    crossed the threshold or is unavailable.
    """

    def get_eou_signal(self) -> TurnSignal | None: ...


class _NullNativeDuplexEouSource:
    """No-op source used while native_duplex is unavailable.

    # UNAVAILABLE: #157 — libcudart blocker, native_duplex EOU unavailable
    Always returns None, routing every EOU decision to the SmartTurn v3 /
    VAD fallback path.
    """

    def get_eou_signal(self) -> TurnSignal | None:
        # UNAVAILABLE: #157 — libcudart blocker, native_duplex EOU unavailable
        return None
