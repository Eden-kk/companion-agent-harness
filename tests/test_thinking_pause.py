"""Stage 1 — "I think..." + 1.5s silence + continuation produces no premature full_response.

See docs/architecture-v0.1.md §Part 6 Stage 1, fixture thinking_pause_001 in
§Part 6c, and §Part 8 v0.1a acceptance gate
(thinking_pause_false_positive_rate = 0 on fixture set).

Deferred to v0.1b — see issue #10.
"""

import pytest


def test_thinking_pause():
    pytest.skip(
        "Deferred to v0.1b: test_thinking_pause requires SmartTurnDetector to "
        "distinguish a thinking pause from end-of-utterance. VAD-only (v0.1a) "
        "cannot satisfy it — same rationale spec Part 8 gives for deferring "
        "test_backchannel_survival / test_detector_ablation. See issue #10."
    )
