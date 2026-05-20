"""v0.2b Anchor 1 local-CI guard: pyannote.audio must be importable.

Runs in the base CI matrix (not b200-gated) to catch a broken venv before
dispatching a b200 GPU job.  If pyannote.audio is not installed, this test
fails loudly with an actionable message.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pyannote.audio")


def test_pyannote_audio_importable():
    try:
        import pyannote.audio  # noqa: F401
    except ImportError as exc:
        pytest.fail(
            f"pyannote.audio is not importable: {exc}\n"
            "Run: pip install 'pyannote.audio>=3.0'\n"
            "If on the b200 venv: /raid/yid042/venvs/companion-harness/bin/pip install 'pyannote.audio>=3.0'"
        )
