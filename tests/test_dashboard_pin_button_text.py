"""Pin button uses plain text labels, not emoji."""

from __future__ import annotations

from pathlib import Path

_HTML = (Path(__file__).parent.parent / "manual_test_console" / "index.html").read_text()


def test_no_pin_emoji() -> None:
    assert "\U0001f4cc" not in _HTML  # 📌
    assert "\U0001f4cd" not in _HTML  # 📍


def test_pin_text_label_present() -> None:
    assert '"pin"' in _HTML or "'pin'" in _HTML or 'textContent = isPinned ? "unpin" : "pin"' in _HTML


def test_unpin_text_label_present() -> None:
    assert '"unpin"' in _HTML or "'unpin'" in _HTML
