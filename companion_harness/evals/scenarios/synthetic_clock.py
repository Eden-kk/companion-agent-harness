"""SyntheticClock — deterministic monotonic-counter clock for eval fixtures.

eval-subsystem-spec.md Anchor 6 (synthetic_clock timing mode).
Phase A.5 Task A.5-1.

The orchestrator calls time.monotonic_ns() / time.monotonic() on the hot
path.  Eval drivers that need deterministic timestamps inject a SyntheticClock
and route time queries through it.  No wall-clock reads; no OS calls.
"""

from __future__ import annotations


class SyntheticClock:
    """Monotonic counter clock.  Thread-unsafe by design — eval is single-threaded."""

    def __init__(self, start_ns: int = 0) -> None:
        self._ns = start_ns

    def now_ns(self) -> int:
        return self._ns

    def now_ms(self) -> int:
        return self._ns // 1_000_000

    def advance_ms(self, n: int) -> None:
        self._ns += n * 1_000_000
