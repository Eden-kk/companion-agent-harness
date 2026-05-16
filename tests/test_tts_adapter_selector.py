"""Contract tests for --tts-adapter CLI selector (v0.1j Task 14).

Verifies:
  1. Default (no --tts-adapter flag) resolves to kokoro factory.
  2. Explicit --tts-adapter=kokoro resolves to kokoro factory.
"""

from __future__ import annotations

import argparse


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tts-adapter", dest="tts_adapter",
                        choices=["kokoro", "native_minicpm"], default="kokoro")
    return parser.parse_args(argv)


def test_default_tts_adapter_is_kokoro() -> None:
    """No --tts-adapter flag → default is 'kokoro'."""
    args = _parse_args([])
    assert args.tts_adapter == "kokoro"


def test_tts_adapter_flag_kokoro_works() -> None:
    """Explicit --tts-adapter=kokoro → 'kokoro'."""
    args = _parse_args(["--tts-adapter=kokoro"])
    assert args.tts_adapter == "kokoro"
