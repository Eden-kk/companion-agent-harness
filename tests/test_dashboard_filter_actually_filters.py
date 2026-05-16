"""F3 regression test — legacy renderEvent must not direct-append to signalRows.

Success criterion: pytest tests/test_dashboard_filter_actually_filters.py passes.

  test_signal_rows_append_count_at_most_one
  test_render_event_skips_signal_rows
  test_apply_filter_is_sole_signal_rows_writer
"""

from __future__ import annotations

import re
from pathlib import Path

_HTML = (Path(__file__).parent.parent / "manual_test_console" / "index.html").read_text()

# Extract just the renderEvent function body (from its opening brace to the
# closing brace, heuristically terminated by the next top-level function).
_render_start = _HTML.index("function renderEvent(evt)")
_render_end = _HTML.index("\n  function ", _render_start + 1)
_render_event_body = _HTML[_render_start:_render_end]


def test_signal_rows_append_count_at_most_one() -> None:
    """Only applyFilter/buildSignalRow may appendChild into signalRows.

    The legacy renderEvent direct-append was the F3 root cause.
    After the fix there must be ≤1 signalRows.appendChild(...) in the file
    (the one inside applyFilter).
    """
    matches = re.findall(r"signalRows\.appendChild\(", _HTML)
    assert len(matches) <= 1, (
        f"Expected ≤1 signalRows.appendChild, found {len(matches)}: "
        "legacy renderEvent direct-append may have been re-introduced."
    )


def test_render_event_skips_signal_rows() -> None:
    """renderEvent body must not reference signalRows."""
    assert "signalRows" not in _render_event_body, (
        "renderEvent still references signalRows — F3 direct-append not fully removed."
    )


def test_apply_filter_is_sole_signal_rows_writer() -> None:
    """applyFilter must contain the only signalRows.innerHTML = '' reset."""
    resets = re.findall(r'signalRows\.innerHTML\s*=\s*["\']', _HTML)
    assert len(resets) == 1, (
        f"Expected exactly 1 signalRows.innerHTML reset (in applyFilter), found {len(resets)}."
    )
