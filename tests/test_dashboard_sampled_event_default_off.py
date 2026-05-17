"""display_event_sampled and deictic_classification are suppressed by default with typed previews."""

from __future__ import annotations

from pathlib import Path

_HTML = (Path(__file__).parent.parent / "manual_test_console" / "index.html").read_text()


def test_display_event_sampled_default_off() -> None:
    assert '"display_event_sampled"' in _HTML


def test_deictic_classification_default_off() -> None:
    assert '"deictic_classification"' in _HTML


def test_display_event_sampled_typed_preview() -> None:
    assert 'evt.event_type === "display_event_sampled"' in _HTML
    assert "forwarded" in _HTML
    assert "sampled out" in _HTML


def test_deictic_classification_typed_preview() -> None:
    assert 'evt.event_type === "deictic_classification"' in _HTML
    assert "is_deictic" in _HTML
    assert "conf=" in _HTML
