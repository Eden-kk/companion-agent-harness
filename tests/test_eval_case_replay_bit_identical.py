"""Tier-B replay bit-identical verification (Phase A.5 Task A.5-4).

Run a fixture twice under SyntheticClock + FixtureScenarioDriver; assert the
two event logs produce identical SpeakDecision sequences when fed to the
Tier-B replayer.

Deferred: companion_harness/replay.py contains only a docstring stub.
A.5-5 (optional) or Phase A.5+1 will implement run_tier_b_replay(); un-xfail
this test when that lands.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Tier-B replay.run_tier_b_replay() not yet implemented (Phase A.5+1)")
def test_fixture_run_twice_produces_bit_identical_speak_decisions() -> None:
    """Feed the same fixture twice; assert SpeakDecision sequences are identical.

    When companion_harness.replay.run_tier_b_replay() is implemented:
    1. Build two FixtureScenarioDriver instances with identical SyntheticClock state.
    2. Run case twice, capture event_log from each ReplayRun.
    3. Call run_tier_b_replay(event_log) on each log.
    4. Assert the two SpeakDecision lists are equal (bit-identical reason codes).
    """
    raise NotImplementedError("Phase A.5+1")
