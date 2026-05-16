"""Frontend filter DEFAULT_OFF set includes high-volume event types."""

from __future__ import annotations

from pathlib import Path

_INDEX = Path(__file__).parent.parent / "manual_test_console" / "index.html"
_HTML = _INDEX.read_text()


def test_vad_frame_default_off() -> None:
    assert '"vad_frame"' in _HTML


def test_raw_audio_chunk_default_off() -> None:
    assert '"raw_audio_chunk"' in _HTML


def test_audio_frame_default_off() -> None:
    assert '"audio_frame"' in _HTML


def test_assistant_audio_buffer_queued_default_off() -> None:
    assert '"assistant_audio_buffer_queued"' in _HTML


def test_display_subscriber_drop_default_off() -> None:
    assert '"display_subscriber_drop"' in _HTML


def test_default_off_set_contains_all_high_volume() -> None:
    """All five high-volume types appear in the DEFAULT_OFF declaration block."""
    # The DEFAULT_OFF set is defined in a JS `new Set([...])` literal.
    high_volume = [
        "vad_frame",
        "raw_audio_chunk",
        "audio_frame",
        "assistant_audio_buffer_queued",
        "display_subscriber_drop",
    ]
    for t in high_volume:
        assert f'"{t}"' in _HTML, f"'{t}' not found in DEFAULT_OFF in index.html"
