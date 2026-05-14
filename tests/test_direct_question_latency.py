"""Stage 1 — closed-question prompt gets a full_response promptly (positive responsiveness).

See docs/architecture-v0.1.md §Part 8 — this test exists specifically to
prevent "passes by being sluggish." Acceptance gates:
  direct_question_latency_p50 < 800 ms
  direct_question_latency_p95 < 1500 ms
Fixture: direct_question_001 in §Part 6c.
"""

import pytest


def test_direct_question_latency():
    pytest.skip("not implemented at v0.1a-bootstrap")
