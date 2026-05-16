"""Argparse contract tests for repro_f0a_f0b auto-wait-for-tts mode."""
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent.parent / "tests" / "manual" / "repro_f0a_f0b.py"


def _load_parser():
    spec = importlib.util.spec_from_file_location("repro_f0a_f0b", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["repro_f0a_f0b"] = mod
    spec.loader.exec_module(mod)
    return mod._build_parser()


def test_auto_wait_for_tts_subcommand_exists():
    p = _load_parser()
    args = p.parse_args(["auto-wait-for-tts"])
    assert args.mode == "auto-wait-for-tts"


def test_turn2_delay_default_150():
    p = _load_parser()
    args = p.parse_args(["auto-wait-for-tts"])
    assert args.turn2_delay_after_tts_start_ms == 150


def test_turn2_delay_override():
    p = _load_parser()
    args = p.parse_args(["auto-wait-for-tts", "--turn2-delay-after-tts-start-ms", "300"])
    assert args.turn2_delay_after_tts_start_ms == 300


def test_turn2_text_optional():
    p = _load_parser()
    args = p.parse_args(["auto-wait-for-tts"])
    assert args.turn2_text is None


def test_turn2_text_override():
    p = _load_parser()
    args = p.parse_args(["auto-wait-for-tts", "--turn2-text", "Hello there"])
    assert args.turn2_text == "Hello there"


def test_existing_modes_still_present():
    p = _load_parser()
    for mode in ("passive", "auto"):
        args = p.parse_args([mode])
        assert args.mode == mode
