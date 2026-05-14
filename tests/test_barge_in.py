"""Stage 1 — assistant must stop within 200ms (VAD-detected) on user interruption.

See docs/architecture-v0.1.md §Part 6 Stage 1, §Part 8 v0.1a acceptance gates:
  vad_detected_user_speech_to_stop_ms_p95   < 200 ms (system-internal, strict)
  physical_user_speech_onset_to_stop_ms_p95 < 350 ms (product-felt, v0.1a loose)
Fixture: barge_in_001 in §Part 6c.
"""

import pytest


def test_barge_in():
    pytest.skip("not implemented at v0.1a-bootstrap")
