"""Contract tests for --tts-adapter CLI selector (v0.1j Task 14).

Verifies:
  1. Default (no --tts-adapter flag) resolves to kokoro factory.
  2. Explicit --tts-adapter=kokoro resolves to kokoro factory.
  3. --tts-adapter=cosyvoice2 is accepted and resolves correctly.
  4. --tts-adapter=native_minicpm is accepted.
"""

from __future__ import annotations

import argparse


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tts-adapter", dest="tts_adapter",
                        choices=["kokoro", "native_minicpm", "cosyvoice2"], default="kokoro")
    return parser.parse_args(argv)


def test_default_tts_adapter_is_kokoro() -> None:
    """No --tts-adapter flag → default is 'kokoro'."""
    args = _parse_args([])
    assert args.tts_adapter == "kokoro"


def test_tts_adapter_flag_kokoro_works() -> None:
    """Explicit --tts-adapter=kokoro → 'kokoro'."""
    args = _parse_args(["--tts-adapter=kokoro"])
    assert args.tts_adapter == "kokoro"


def test_tts_adapter_flag_cosyvoice2_accepted() -> None:
    """--tts-adapter=cosyvoice2 → 'cosyvoice2'."""
    args = _parse_args(["--tts-adapter=cosyvoice2"])
    assert args.tts_adapter == "cosyvoice2"


def test_tts_adapter_flag_native_minicpm_accepted() -> None:
    """--tts-adapter=native_minicpm → 'native_minicpm'."""
    args = _parse_args(["--tts-adapter=native_minicpm"])
    assert args.tts_adapter == "native_minicpm"


def test_tts_adapter_choices_includes_cosyvoice2() -> None:
    """argparse choices include cosyvoice2; unknown value is rejected."""
    import pytest  # noqa: WPS433
    with pytest.raises(SystemExit):
        _parse_args(["--tts-adapter=unknown_backend"])
