"""Contract test: _on_startup_finalize_deictic upgrades the adapter label.

N5 regression guard: when --enable-deictic is set (default True since v0.2e)
and the foreground model exposes .chat(), the startup hook must replace the
"stub:_NullDeicticModel" label with "real:MiniCPMDeicticDetector" and store a
real MiniCPMDeicticDetector on the app.

Without this test the PR #294 flip from default-off to default-on was silently
ineffective because MiniCPMStreamingModel (the live pipeline model) lacked
.chat(), causing the startup hook to fall back to stub unconditionally.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestServer

from manual_test_console.server import (
    KEY_ADAPTER_LABELS,
    KEY_DEICTIC_MODEL,
    build_app,
)


class _FakeForegroundWithChat:
    """Minimal foreground model stub that exposes .chat() — no torch needed."""

    def chat(self, text: str, max_new_tokens: int = 8) -> str:
        return "no 0.0"

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> Any:
        return None

    def set_context(self, items: list) -> None:
        pass


class _FakeForegroundWithoutChat:
    """Foreground model stub WITHOUT .chat() — matches old MiniCPMStreamingModel."""

    def infer(self, audio_frame: bytes, video_frame: bytes | None = None) -> Any:
        return None

    def set_context(self, items: list) -> None:
        pass


@pytest.mark.asyncio
async def test_startup_finalize_deictic_upgrades_when_model_has_chat(tmp_path: Path) -> None:
    """When the foreground model has .chat(), the startup hook wires a real
    MiniCPMDeicticDetector and stamps the label 'real:MiniCPMDeicticDetector'."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeForegroundWithChat(),
    )
    # Simulate what main() does after build_app when --enable-deictic is True.
    app[KEY_ADAPTER_LABELS]["deictic_model"] = "real:pending-foreground-load"

    server = TestServer(app)
    await server.start_server()
    try:
        assert app[KEY_ADAPTER_LABELS]["deictic_model"] == "real:MiniCPMDeicticDetector"
        assert app[KEY_DEICTIC_MODEL] is not None
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_startup_finalize_deictic_falls_back_when_model_lacks_chat(tmp_path: Path) -> None:
    """When the foreground model lacks .chat(), the startup hook falls back to stub.

    This was the silent failure mode before N5 was fixed: MiniCPMStreamingModel
    did not expose .chat(), so the deictic upgrade never happened.
    """
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeForegroundWithoutChat(),
    )
    app[KEY_ADAPTER_LABELS]["deictic_model"] = "real:pending-foreground-load"

    server = TestServer(app)
    await server.start_server()
    try:
        assert app[KEY_ADAPTER_LABELS]["deictic_model"] == "stub:_NullDeicticModel"
        assert app[KEY_DEICTIC_MODEL] is None
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_startup_finalize_deictic_skipped_when_label_is_stub(tmp_path: Path) -> None:
    """When label stays 'stub:...' (--no-enable-deictic path), the hook is a no-op."""
    app = build_app(
        blob_dir=tmp_path / "blobs",
        live_pipeline_enabled=True,
        foreground_model=_FakeForegroundWithChat(),
    )
    # Do NOT override the label — it stays "stub:_NullDeicticModel" from build_app.

    server = TestServer(app)
    await server.start_server()
    try:
        assert app[KEY_ADAPTER_LABELS]["deictic_model"] == "stub:_NullDeicticModel"
        assert app[KEY_DEICTIC_MODEL] is None
    finally:
        await server.close()


def test_minicpm_streaming_model_has_chat_method() -> None:
    """MiniCPMStreamingModel must expose .chat() so _on_startup_finalize_deictic
    can wire MiniCPMDeicticDetector against the live pipeline model (N5 fix).

    Inspects the source without importing the module (torch not available
    outside b200). The b200 integration test exercises real inference.
    """
    import ast
    import inspect
    from pathlib import Path

    src_path = Path(__file__).parent.parent / "companion_harness" / "foreground_model_minicpm.py"
    tree = ast.parse(src_path.read_text())

    streaming_class = next(
        (node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "MiniCPMStreamingModel"),
        None,
    )
    assert streaming_class is not None, "MiniCPMStreamingModel class not found in foreground_model_minicpm.py"

    method_names = {
        node.name for node in ast.walk(streaming_class) if isinstance(node, ast.FunctionDef)
    }
    assert "chat" in method_names, (
        "MiniCPMStreamingModel must have a .chat() method so "
        "_on_startup_finalize_deictic can wire MiniCPMDeicticDetector "
        "(N5 regression: deictic default-on flip was silently ineffective)"
    )

    chat_def = next(
        node for node in ast.walk(streaming_class)
        if isinstance(node, ast.FunctionDef) and node.name == "chat"
    )
    param_names = [arg.arg for arg in chat_def.args.args]
    assert "text" in param_names, "MiniCPMStreamingModel.chat must accept 'text' parameter"
    assert "max_new_tokens" in param_names, "MiniCPMStreamingModel.chat must accept 'max_new_tokens' parameter"
