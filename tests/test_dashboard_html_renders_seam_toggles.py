"""D3 smoke test — index.html contains the Hot seams toggle panel.

Success criterion: pytest tests/test_dashboard_html_renders_seam_toggles.py passes.

  test_hot_seams_section_exists
  test_fetch_model_swap_call_present
  test_no_cold_seams_section
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path


_INDEX = Path(__file__).parent.parent / "manual_test_console" / "index.html"
_HTML = _INDEX.read_text()


class _AttrCollector(HTMLParser):
    """Collect tag→attrs for assertion."""

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.seam_rows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        if "id" in d and d["id"]:
            self.ids.add(d["id"])
        if d.get("data-seam"):
            self.seam_rows.append(d)


def _parse() -> _AttrCollector:
    c = _AttrCollector()
    c.feed(_HTML)
    return c


def test_hot_seams_section_exists() -> None:
    c = _parse()
    assert "hot-seams-section" in c.ids


def test_fetch_model_swap_call_present() -> None:
    assert 'fetch("/config/model-swap"' in _HTML


def test_no_cold_seams_section() -> None:
    c = _parse()
    assert "cold-seams-section" not in c.ids
