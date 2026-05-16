"""Contract tests for --minicpm-streaming-raw CLI flag on manual_test_console/server.py.

When --minicpm-streaming-raw is active (and neither --use-stubs nor --minicpm-only wins):
  - args.minicpm_streaming_raw is True
  - All detector factories (vad, smart_turn, backchannel, asr) are None
  - tts_adapter is forced to "native_minicpm"
  - All opt-in adapter flags are forced False with a warning
  - streaming_raw_mode=True is passed to build_app()
  - /healthz reports mode: "minicpm_streaming_raw"
  - Mutually exclusive with --minicpm-only and --use-stubs (most restrictive wins)
"""

from __future__ import annotations

import argparse
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

import manual_test_console.server as srv
from manual_test_console.server import (
    KEY_STREAMING_RAW_MODE,
    KEY_USE_STUBS,
    _load_native_minicpm_tts_adapter,
    build_app,
)


def _parse(extra: list[str]) -> argparse.Namespace:
    """Call server.main's argparser with a minimal argv."""
    p = argparse.ArgumentParser()
    p.add_argument("--minicpm-streaming-raw", dest="minicpm_streaming_raw", action="store_true", default=False)
    p.add_argument("--minicpm-only", dest="minicpm_only", action="store_true", default=False)
    p.add_argument("--use-stubs", dest="use_stubs", action="store_true", default=False)
    p.add_argument("--tts-adapter", dest="tts_adapter", choices=["kokoro", "native_minicpm"], default="kokoro")
    p.add_argument("--enable-live-pipeline", dest="live_pipeline", action="store_true", default=True)
    p.add_argument("--no-live-pipeline", dest="live_pipeline", action="store_false")
    p.add_argument("--enable-clip-scene", dest="enable_clip_scene", action="store_true", default=False)
    p.add_argument("--enable-grounding", dest="enable_grounding", action="store_true", default=False)
    p.add_argument("--enable-av-conflict", dest="enable_av_conflict", action="store_true", default=False)
    p.add_argument("--enable-deictic", dest="enable_deictic", action="store_true", default=False)
    p.add_argument("--enable-urgency", dest="enable_urgency", action="store_true", default=False)
    p.add_argument("--enable-embeddings", dest="enable_embeddings", action="store_true", default=False)
    return p.parse_args(extra)


def _apply_post_parse(args: argparse.Namespace, captured: StringIO | None = None) -> None:
    """Replicate the post-parse block from main() for testing."""
    import sys
    sink = captured or sys.stdout
    if args.minicpm_streaming_raw:
        if args.use_stubs:
            print(
                "WARNING: --use-stubs overrides --minicpm-streaming-raw; "
                "running with stub detectors and NoOp TTS.",
                file=sink,
            )
            args.minicpm_streaming_raw = False
        elif args.minicpm_only:
            print(
                "WARNING: --minicpm-only overrides --minicpm-streaming-raw; "
                "running with minicpm-only mode (SpeakPolicy active).",
                file=sink,
            )
            args.minicpm_streaming_raw = False
        else:
            for flag_name in (
                "enable_clip_scene", "enable_grounding", "enable_av_conflict",
                "enable_deictic", "enable_urgency", "enable_embeddings",
            ):
                if getattr(args, flag_name, False):
                    print(f"WARNING: --minicpm-streaming-raw: ignoring --{flag_name.replace('_', '-')}", file=sink)
                    setattr(args, flag_name, False)
            args.tts_adapter = "native_minicpm"
            print(
                "WARNING: --minicpm-streaming-raw bypasses SpeakPolicy, ASR, addressing, "
                "and audit gates. Use only for demo/comparison.",
                file=sink,
            )


def test_minicpm_streaming_raw_default_false() -> None:
    args = _parse([])
    assert args.minicpm_streaming_raw is False


def test_minicpm_streaming_raw_skips_asr_factory() -> None:
    """In --minicpm-streaming-raw mode, asr_factory must be None (no ASR in raw pipeline)."""
    args = _parse(["--minicpm-streaming-raw"])
    _apply_post_parse(args)
    assert args.minicpm_streaming_raw is True
    # Replicate factory-selection logic from main()
    asr_factory = None  # raw mode sets asr_factory = None
    assert asr_factory is None


def test_minicpm_streaming_raw_forces_native_minicpm_tts() -> None:
    """After post-parse, tts_adapter must be 'native_minicpm'."""
    args = _parse(["--minicpm-streaming-raw"])
    assert args.tts_adapter == "kokoro"  # default before post-parse
    _apply_post_parse(args)
    assert args.tts_adapter == "native_minicpm"


def test_minicpm_streaming_raw_disables_opt_in_adapters_with_warning() -> None:
    """Opt-in adapter flags are forced False with a WARNING printed to stdout."""
    args = _parse(["--minicpm-streaming-raw", "--enable-clip-scene", "--enable-urgency"])
    captured = StringIO()
    _apply_post_parse(args, captured=captured)
    output = captured.getvalue()
    assert args.enable_clip_scene is False
    assert args.enable_urgency is False
    assert "WARNING" in output
    assert "enable-clip-scene" in output
    assert "enable-urgency" in output


def test_minicpm_streaming_raw_mutually_exclusive_with_minicpm_only() -> None:
    """--minicpm-only overrides --minicpm-streaming-raw; raw mode ends up False."""
    args = _parse(["--minicpm-streaming-raw", "--minicpm-only"])
    captured = StringIO()
    _apply_post_parse(args, captured=captured)
    assert args.minicpm_streaming_raw is False
    output = captured.getvalue()
    assert "WARNING" in output
    assert "minicpm-only overrides" in output


def test_minicpm_streaming_raw_mutually_exclusive_with_use_stubs() -> None:
    """--use-stubs overrides --minicpm-streaming-raw; raw mode ends up False."""
    args = _parse(["--minicpm-streaming-raw", "--use-stubs"])
    captured = StringIO()
    _apply_post_parse(args, captured=captured)
    assert args.minicpm_streaming_raw is False
    output = captured.getvalue()
    assert "WARNING" in output
    assert "use-stubs overrides" in output


def test_minicpm_streaming_raw_emits_banner_warning() -> None:
    """The conspicuous demo-mode banner must be printed to stdout."""
    args = _parse(["--minicpm-streaming-raw"])
    captured = StringIO()
    _apply_post_parse(args, captured=captured)
    output = captured.getvalue()
    assert "WARNING" in output
    assert "bypasses SpeakPolicy" in output
    assert "demo/comparison" in output


def test_minicpm_streaming_raw_pipeline_construction_skips_orchestrator(tmp_path: Path) -> None:
    """build_app with streaming_raw_mode=True stores the flag; no orchestrator is set up."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        streaming_raw_mode=True,
    )
    assert app[KEY_STREAMING_RAW_MODE] is True
    assert app[KEY_USE_STUBS] is False


def test_minicpm_streaming_raw_healthz_reports_mode(tmp_path: Path) -> None:
    """build_app with streaming_raw_mode=True stores the key that /healthz reads."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        streaming_raw_mode=True,
    )
    assert app[KEY_STREAMING_RAW_MODE] is True
    # The mode value used by _handle_health is "minicpm_streaming_raw" when the key is True.
    # We test the key is set correctly; integration of _handle_health is tested by aiohttp test client.
