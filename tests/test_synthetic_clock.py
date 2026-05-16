"""Tests for SyntheticClock (Phase A.5 Task A.5-1)."""

from companion_harness.evals.scenarios.synthetic_clock import SyntheticClock


def test_initial_now_ns_is_zero() -> None:
    clock = SyntheticClock()
    assert clock.now_ns() == 0


def test_initial_now_ms_is_zero() -> None:
    clock = SyntheticClock()
    assert clock.now_ms() == 0


def test_advance_ms_increments_ns_correctly() -> None:
    clock = SyntheticClock()
    clock.advance_ms(50)
    assert clock.now_ns() == 50_000_000
    assert clock.now_ms() == 50


def test_multiple_advances_accumulate() -> None:
    clock = SyntheticClock()
    clock.advance_ms(10)
    clock.advance_ms(20)
    clock.advance_ms(30)
    assert clock.now_ms() == 60


def test_deterministic_same_sequence_same_result() -> None:
    def run() -> list[int]:
        c = SyntheticClock(start_ns=1_000_000)
        snapshots = []
        for step in (5, 10, 15):
            c.advance_ms(step)
            snapshots.append(c.now_ms())
        return snapshots

    assert run() == run()


def test_custom_start_ns() -> None:
    clock = SyntheticClock(start_ns=1_000_000_000)
    assert clock.now_ms() == 1000
