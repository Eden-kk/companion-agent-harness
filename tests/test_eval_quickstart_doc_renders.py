"""Tests for Task A7: docs/eval-quickstart.md exists and covers required sections."""

from pathlib import Path

_DOC = Path(__file__).parent.parent / "docs" / "eval-quickstart.md"


def test_eval_quickstart_exists() -> None:
    assert _DOC.exists(), "docs/eval-quickstart.md missing"


def test_eval_quickstart_includes_install_cmd() -> None:
    text = _DOC.read_text()
    assert "pip install -e .[eval]" in text


def test_eval_quickstart_includes_run_cmd() -> None:
    text = _DOC.read_text()
    assert "--adapter harness_native" in text
    assert "--output" in text


def test_eval_quickstart_anchors_listed() -> None:
    text = _DOC.read_text()
    assert "Import direction" in text
    assert "Tier-B replay" in text
    assert "Schema split" in text
    assert "[eval]" in text
