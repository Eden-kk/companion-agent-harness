import pathlib
import re

HTML = (pathlib.Path(__file__).parent.parent / "manual_test_console" / "index.html").read_text()

# Split on the buildSignalRow definition so we can target the active render site.
_before_build, _build_and_after = HTML.split("function buildSignalRow(", 1)


def test_verbose_metadata_not_in_body_innerhtml_active_site():
    """The active render site (buildSignalRow) must not set body.innerHTML to the old ts/src/seq noise."""
    # Old pattern: body.innerHTML = `<small>...src=${evt.source}...`
    assert 'body.innerHTML = `<small>' not in _build_and_after


def test_event_preview_function_defined():
    assert "function eventPreview(evt)" in HTML


def test_row_title_tooltip_present():
    # Both render sites must set row.title (metadata moved to tooltip).
    assert HTML.count("row.title =") >= 2
