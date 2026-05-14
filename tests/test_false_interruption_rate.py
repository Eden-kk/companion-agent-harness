"""Stage 1 — 10 min scripted long utterances with fillers produce <1 false interruption.

See docs/architecture-v0.1.md §Part 6 Stage 1 and §Part 8 v0.1a acceptance gate
(false_interruption_count_per_10_min < 1).
"""

import pytest


def test_false_interruption_rate():
    pytest.skip("not implemented at v0.1a-bootstrap")
