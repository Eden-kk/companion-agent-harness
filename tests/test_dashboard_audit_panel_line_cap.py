"""F2 (cap) smoke test — index.html contains the retained-line cap for the audit panel.

Success criterion: pytest tests/test_dashboard_audit_panel_line_cap.py passes.

  test_max_audit_rows_constant_present
  test_cap_trim_logic_present
  test_cap_select_present
  test_cap_localstorage_key_present
"""

from __future__ import annotations

from pathlib import Path


_INDEX = Path(__file__).parent.parent / "manual_test_console" / "index.html"
_HTML = _INDEX.read_text()


def test_max_audit_rows_constant_present() -> None:
    assert "MAX_AUDIT_ROWS = 500" in _HTML


def test_cap_trim_logic_present() -> None:
    # The cap is enforced by slicing normal events to auditRowCap before DOM rebuild.
    assert "auditRowCap" in _HTML
    assert "slice(-auditRowCap)" in _HTML


def test_cap_select_present() -> None:
    assert 'id="filter-cap"' in _HTML
    # Options must include 100, 500, 1000, and unlimited (0).
    for val in ("100", "500", "1000", '0'):
        assert f'value="{val}"' in _HTML, f"expected cap option value={val!r}"


def test_cap_localstorage_key_present() -> None:
    # The cap value must be written to and read from the auditFilter localStorage entry.
    assert "cap: auditRowCap" in _HTML
    assert "saved.cap" in _HTML
