"""Async, non-blocking event logger + replay infrastructure.

See docs/architecture-v0.1.md §Part 2 invariant #10 (logger never blocks the
realtime path; emits log_drop_or_degrade on backpressure) and §Part 6
Stage 0 (replay + causal provenance contract tests).
"""
