"""F2 smoke test — index.html contains the audit-panel event filter UI.

Success criterion: pytest tests/test_dashboard_event_filter_ui.py passes.

  test_filter_bar_exists
  test_search_input_present
  test_default_off_types_in_js
  test_default_on_types_in_js
  test_localstorage_persistence_present
  test_filter_counter_present
  test_pin_support_present
  test_reason_code_filter_present
"""

from __future__ import annotations

from pathlib import Path


_INDEX = Path(__file__).parent.parent / "manual_test_console" / "index.html"
_HTML = _INDEX.read_text()


def test_filter_bar_exists() -> None:
    assert 'id="audit-filter-bar"' in _HTML


def test_search_input_present() -> None:
    assert 'id="filter-search"' in _HTML


def test_default_off_types_in_js() -> None:
    for t in ("vad_frame", "raw_audio_chunk", "audio_frame"):
        assert f'"{t}"' in _HTML, f"expected default-OFF type '{t}' in JS"


def test_default_on_types_in_js() -> None:
    for t in ("policy_decision", "speak_decision", "model_swap_completed", "model_swap_rejected"):
        assert f'"{t}"' in _HTML, f"expected default-ON type '{t}' in JS"


def test_localstorage_persistence_present() -> None:
    assert "localStorage.setItem" in _HTML
    assert "localStorage.getItem" in _HTML


def test_filter_counter_present() -> None:
    assert 'id="filter-counter"' in _HTML
    assert "Showing" in _HTML


def test_pin_support_present() -> None:
    assert "pinnedIds" in _HTML
    assert "pinned" in _HTML


def test_reason_code_filter_present() -> None:
    assert 'id="filter-rc-btn"' in _HTML
    assert "primary_reason_code" in _HTML
