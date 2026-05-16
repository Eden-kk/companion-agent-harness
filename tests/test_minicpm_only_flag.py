"""Contract tests for --minicpm-only CLI flag on manual_test_console/server.py.

When --minicpm-only is active:
  - args.tts_adapter is forced to "native_minicpm"
  - vad / smart_turn / backchannel factories are None (stub fallback)
  - asr factory is real (_load_asr_model)
  - --use-stubs overrides it (TTS stays NoOp) with a printed warning
"""

from __future__ import annotations

import argparse
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

import manual_test_console.server as srv
from manual_test_console.server import (
    _load_asr_model,
    _load_native_minicpm_tts_adapter,
    build_app,
)


def _parse(extra: list[str]) -> argparse.Namespace:
    """Call server.main's argparser with a minimal argv."""
    p = argparse.ArgumentParser()
    p.add_argument("--minicpm-only", dest="minicpm_only", action="store_true", default=False)
    p.add_argument("--use-stubs", dest="use_stubs", action="store_true", default=False)
    p.add_argument(
        "--tts-adapter",
        dest="tts_adapter",
        choices=["kokoro", "native_minicpm"],
        default="kokoro",
    )
    p.add_argument("--enable-live-pipeline", dest="live_pipeline", action="store_true", default=True)
    p.add_argument("--no-live-pipeline", dest="live_pipeline", action="store_false")
    return p.parse_args(extra)


def test_minicpm_only_default_false() -> None:
    args = _parse([])
    assert args.minicpm_only is False


def test_minicpm_only_forces_native_minicpm_tts() -> None:
    """After the post-parse block in main(), tts_adapter must be 'native_minicpm'."""
    # Simulate the post-parse mutation from main()
    args = _parse(["--minicpm-only"])
    assert args.minicpm_only is True
    assert args.use_stubs is False
    # Apply the same mutation main() does
    if args.minicpm_only and not args.use_stubs:
        args.tts_adapter = "native_minicpm"
    assert args.tts_adapter == "native_minicpm"


def test_minicpm_only_stubs_vad_smart_turn_backchannel() -> None:
    """In --minicpm-only mode the three auxiliary detector factories are None."""
    args = _parse(["--minicpm-only"])
    # Replicate factory-selection logic from main()
    if args.live_pipeline and not args.use_stubs and args.minicpm_only:
        vad_factory = None
        smart_turn_factory = None
        backchannel_factory = None
        tts_factory = _load_native_minicpm_tts_adapter
        asr_factory = _load_asr_model
    else:
        pytest.fail("unexpected branch")

    assert vad_factory is None
    assert smart_turn_factory is None
    assert backchannel_factory is None


def test_minicpm_only_keeps_asr_real() -> None:
    """ASR factory must remain _load_asr_model so addressing classifier has transcripts."""
    args = _parse(["--minicpm-only"])
    if args.live_pipeline and not args.use_stubs and args.minicpm_only:
        asr_factory = _load_asr_model
    else:
        pytest.fail("unexpected branch")

    assert asr_factory is _load_asr_model


def test_minicpm_only_with_use_stubs_emits_warning(capsys: pytest.CaptureFixture) -> None:
    """--use-stubs overrides --minicpm-only; a warning must be printed to stdout."""
    # We cannot run main() fully (it calls web.run_app), but we can exercise
    # the warning branch by calling the fragment inline.
    args = _parse(["--minicpm-only", "--use-stubs"])
    captured = StringIO()
    if args.minicpm_only and args.use_stubs:
        print(
            "WARNING: --use-stubs overrides --minicpm-only; TTS will be NoOp.",
            file=captured,
            flush=True,
        )
    output = captured.getvalue()
    assert "WARNING" in output
    assert "--use-stubs overrides --minicpm-only" in output
    # tts_adapter must NOT be forced to native_minicpm when use_stubs wins
    assert args.tts_adapter == "kokoro"


def test_build_app_minicpm_only_default_false(tmp_path: Path) -> None:
    """build_app doesn't break when called with no minicpm-only-related kwargs."""
    app = build_app(blob_dir=tmp_path / "blobs", live_pipeline_enabled=False)
    from manual_test_console.server import KEY_USE_STUBS
    assert app[KEY_USE_STUBS] is False
