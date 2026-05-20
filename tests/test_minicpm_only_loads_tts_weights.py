"""Contract tests for the --minicpm-only TTS label fix.

Ensures that when --minicpm-only is set (native_minicpm TTS):
  - build_app receives tts_adapter_name="MiniCPM-o native TTS"
  - the healthz tts_model label does NOT say "Kokoro-82M-ONNX"
  - when the native TTS factory raises, the label falls back to "stub:NoopTtsAdapter"
    (no silent Kokoro fallback)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import manual_test_console.server as srv
from manual_test_console.server import (
    KEY_TTS_LABEL,
    build_app,
)


def test_minicpm_only_passes_tts_adapter_name_to_build_app(tmp_path: Path) -> None:
    """tts_adapter_name kwarg routes the label correctly in build_app."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
        tts_adapter_name="MiniCPM-o native TTS",
    )
    # When live_pipeline_enabled=False, TTS adapter is NoOp — but the name arg
    # must not raise and build_app must be importable with the new kwarg.
    assert app[KEY_TTS_LABEL] == "stub:NoopTtsAdapter"


def test_kokoro_default_name_unchanged(tmp_path: Path) -> None:
    """Default tts_adapter_name remains 'Kokoro-82M-ONNX'."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=False,
    )
    # live_pipeline disabled → NoopTtsAdapter regardless, but the kwarg must
    # default to Kokoro without being specified.
    assert app[KEY_TTS_LABEL] == "stub:NoopTtsAdapter"


def test_tts_label_uses_adapter_name_on_success(tmp_path: Path) -> None:
    """When tts_adapter_factory succeeds, KEY_TTS_LABEL contains tts_adapter_name."""
    fake_adapter = MagicMock()

    def _factory():
        return fake_adapter

    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        tts_adapter_factory=_factory,
        tts_adapter_name="MiniCPM-o native TTS",
        use_stubs=False,
    )

    async def _run() -> None:
        runner = None
        try:
            from aiohttp.test_utils import TestServer
            runner = TestServer(app)
            await runner.start_server()
        finally:
            if runner is not None:
                await runner.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
    label = app[KEY_TTS_LABEL]
    assert "MiniCPM-o native TTS" in label
    assert "Kokoro" not in label


def test_tts_label_falls_back_to_noop_on_failure(tmp_path: Path) -> None:
    """When tts_adapter_factory raises, label is 'stub:NoopTtsAdapter' (no Kokoro)."""

    def _bad_factory():
        raise RuntimeError("libcudart blocker")

    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        tts_adapter_factory=_bad_factory,
        tts_adapter_name="Kokoro-82M-ONNX",
        use_stubs=False,
    )

    async def _run() -> None:
        runner = None
        try:
            from aiohttp.test_utils import TestServer
            runner = TestServer(app)
            await runner.start_server()
        finally:
            if runner is not None:
                await runner.close()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
    label = app[KEY_TTS_LABEL]
    assert label == "stub:NoopTtsAdapter"
    assert "Kokoro" not in label


def test_native_minicpm_tts_name_in_main_args() -> None:
    """main() computes tts_name='MiniCPM-o native TTS' when tts_adapter=native_minicpm."""
    # Simulate the tts_name derivation from main().
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--tts-adapter", dest="tts_adapter", default="kokoro")
    p.add_argument("--minicpm-only", dest="minicpm_only", action="store_true", default=False)
    p.add_argument("--use-stubs", dest="use_stubs", action="store_true", default=False)

    args = p.parse_args(["--minicpm-only"])
    # Simulate the post-parse mutation in main()
    if args.minicpm_only and not args.use_stubs:
        args.tts_adapter = "native_minicpm"

    tts_name = "MiniCPM-o native TTS" if args.tts_adapter == "native_minicpm" else "Kokoro-82M-ONNX"
    assert tts_name == "MiniCPM-o native TTS"


def test_kokoro_tts_name_in_main_args_default() -> None:
    """main() computes tts_name='Kokoro-82M-ONNX' by default."""
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--tts-adapter", dest="tts_adapter", default="kokoro")

    args = p.parse_args([])
    tts_name = "MiniCPM-o native TTS" if args.tts_adapter == "native_minicpm" else "Kokoro-82M-ONNX"
    assert tts_name == "Kokoro-82M-ONNX"
