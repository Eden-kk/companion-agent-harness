"""Stage 1 — "I think..." + 1.5s silence + continuation produces no premature full_response.

See docs/architecture-v0.1.md §Part 6 Stage 1, fixture thinking_pause_001 in
§Part 6c, and §Part 8 v0.1a acceptance gate
(thinking_pause_false_positive_rate = 0 on fixture set).
"""

import pytest


def test_thinking_pause():
    pytest.skip("not implemented at v0.1a-bootstrap")
