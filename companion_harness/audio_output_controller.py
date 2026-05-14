"""AudioOutputController — playback lifecycle, barge-in stop, generation cancel.

See docs/architecture-v0.1.md §Part 3 (adapter interfaces) and §Part 5 for the
new event_types this adapter emits (assistant_generation_start,
assistant_audio_buffer_queued/flushed, assistant_audio_stop_requested/completed,
log_drop_or_degrade). Stop path is gated by §Part 8 v0.1a barge-in latencies.
"""
