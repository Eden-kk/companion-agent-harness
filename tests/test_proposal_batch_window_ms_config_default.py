"""R2-1: ConfigStore default for proposal_batch_window_ms must be 600, max 1500.

Without this, _snapshot_config() in realtime_orchestrator.py overwrites the
build_live_pipeline kwarg on every EOU, silently restoring the old 80 ms value
and making the PR #306 fix dead code.
"""

from manual_test_console.config_schema import ALLOWLIST


def test_proposal_batch_window_ms_default_is_600():
    entry = ALLOWLIST["orchestrator.proposal_batch_window_ms"]
    assert entry.default == 600, (
        f"expected default=600 (covers b200 2nd-turn latency), got {entry.default}"
    )


def test_proposal_batch_window_ms_max_is_1500():
    entry = ALLOWLIST["orchestrator.proposal_batch_window_ms"]
    assert entry.max == 1500, (
        f"expected max=1500, got {entry.max}"
    )
