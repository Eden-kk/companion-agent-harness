"""Stage 0 — reconstructed DAG has no orphan actions.

See docs/architecture-v0.1.md §Part 6 Stage 0 and §Part 8 v0.1a acceptance gate
(orphan_action_count = 0). Every action must trace to user input or a
scheduled trigger.
"""

import pytest


def test_causal_graph_completeness():
    pytest.skip("not implemented at v0.1a-bootstrap")
