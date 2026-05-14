"""Replay harness — Tier A (end-to-end behavioral) and Tier B (policy-layer exact).

See docs/architecture-v0.1.md §Part 2 invariants #5-#6 and §Part 6 Stage 0 for
the two replay tiers. Tier B replay is bit-identical; Tier A uses the behavioral
tuple (same_action_class + same_timing_bucket(+/-200ms) + same_interaction_intent
+ same_safety_class).
"""
