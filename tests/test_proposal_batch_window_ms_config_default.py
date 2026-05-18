"""R2-1: ConfigStore default for proposal_batch_window_ms must be 2500, max >=2500.

Without this, _snapshot_config() in realtime_orchestrator.py overwrites the
build_live_pipeline kwarg on every EOU, silently restoring the old 80 ms value
and making the PR #306 fix dead code.
"""

from manual_test_console.config_schema import ALLOWLIST


def test_proposal_batch_window_ms_default_is_2500():
    entry = ALLOWLIST["orchestrator.proposal_batch_window_ms"]
    assert entry.default == 2500, (
        f"expected default=2500 (matches MiniCPM-o measured chunk latency), got {entry.default}"
    )


def test_proposal_batch_window_ms_max_covers_default():
    entry = ALLOWLIST["orchestrator.proposal_batch_window_ms"]
    assert entry.max >= 2500, (
        f"expected max>=2500, got {entry.max}"
    )
