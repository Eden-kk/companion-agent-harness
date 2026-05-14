"""VADDetector — the single end-of-utterance detector enabled in v0.1a.

See docs/architecture-v0.1.md §Part 3 TurnDetectorSuite (v0.1a uses VAD only;
SmartTurn / SemanticEOU / NativeDuplex are deferred to v0.1b) and §Part 8
for the v0.1a adapter scope. Emits TurnSignal per §Part 5.
"""
