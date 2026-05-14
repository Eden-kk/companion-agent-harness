"""Stage 0 — every assistant_audio_start has a non-empty caused_by chain.

See docs/architecture-v0.1.md §Part 6 Stage 0 and §Part 8 v0.1a acceptance gate
(assistant_audio_start_with_cause = 100%).
"""

import pytest


def test_decision_provenance():
    pytest.skip("not implemented at v0.1a-bootstrap")
