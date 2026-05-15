"""Manual-test web console — Phase 1 (audio capture + observability).

See docs/manual-test-module-plan-draft.md for the module plan and
docs/visionclaw-adaptation-plan-draft.md §4 for the wire contract.

This module wires InputIngest (companion_harness.input_ingest) to a browser
page over plain WebSocket. Phase 1 is observability-only — no synthesized
voice output.
"""
