"""SpeakPolicy — action-type decision from signals; v0.1a output = {silence, full_response}.

See docs/architecture-v0.1.md §Part 6 Stage 3 (speak/silence policy, rules-first,
silence wins ties) and §Part 8 (v0.1a restricts the action set to two values).
Every SpeakDecision must carry a primary_reason_code from ReasonCode — no
free-text reasoning on the policy path (invariant #5 / Stage 0 Tier B replay).
"""
