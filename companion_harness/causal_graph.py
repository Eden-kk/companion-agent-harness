"""Offline causal-graph reconstruction from caused_by[] edges on Events.

See docs/architecture-v0.1.md §Part 6 Stage 0 — test_causal_graph_completeness
requires that every action traces to either user input or a scheduled trigger;
orphan events are a Stage 0 contract failure.
"""
