"""Contract test: server.py _construct_background_reasoner env-var dispatcher (T4).

Success criterion (T4):
  All four cases pass: default fake, mcp+url, mcp-no-url, unknown choice.
"""

from __future__ import annotations

import importlib
import os
from unittest.mock import patch

import pytest

from companion_harness.background_reasoner import FakeBackgroundReasoner, MCPBackgroundReasoner


def _dispatch(env: dict) -> object:
    """Call _construct_background_reasoner with the given env."""
    from manual_test_console.server import _construct_background_reasoner
    with patch.dict(os.environ, env, clear=False):
        # Remove keys not in env
        keys_to_remove = [k for k in ("BACKGROUND_REASONER", "MCP_SERVER_URL") if k not in env]
        for k in keys_to_remove:
            os.environ.pop(k, None)
        return _construct_background_reasoner()


def test_default_env_constructs_fake_reasoner() -> None:
    """No BACKGROUND_REASONER env → FakeBackgroundReasoner."""
    env = {}
    with patch.dict(os.environ, env, clear=False):
        os.environ.pop("BACKGROUND_REASONER", None)
        os.environ.pop("MCP_SERVER_URL", None)
        from manual_test_console.server import _construct_background_reasoner
        result = _construct_background_reasoner()
    assert isinstance(result, FakeBackgroundReasoner)


def test_fake_env_constructs_fake_reasoner() -> None:
    """BACKGROUND_REASONER=fake → FakeBackgroundReasoner."""
    with patch.dict(os.environ, {"BACKGROUND_REASONER": "fake"}, clear=False):
        from manual_test_console.server import _construct_background_reasoner
        result = _construct_background_reasoner()
    assert isinstance(result, FakeBackgroundReasoner)


def test_mcp_env_without_url_raises() -> None:
    """BACKGROUND_REASONER=mcp with no MCP_SERVER_URL → RuntimeError."""
    with patch.dict(os.environ, {"BACKGROUND_REASONER": "mcp"}, clear=False):
        os.environ.pop("MCP_SERVER_URL", None)
        from manual_test_console.server import _construct_background_reasoner
        with pytest.raises(RuntimeError, match="MCP_SERVER_URL"):
            _construct_background_reasoner()


def test_mcp_env_constructs_mcp_reasoner() -> None:
    """BACKGROUND_REASONER=mcp + MCP_SERVER_URL → MCPBackgroundReasoner with correct url."""
    url = "stdio:///fake/server.py"
    with patch.dict(
        os.environ,
        {"BACKGROUND_REASONER": "mcp", "MCP_SERVER_URL": url},
        clear=False,
    ):
        from manual_test_console.server import _construct_background_reasoner
        result = _construct_background_reasoner()
    assert isinstance(result, MCPBackgroundReasoner)
    assert result._mcp_server_url == url


def test_unknown_env_raises() -> None:
    """BACKGROUND_REASONER=potato → RuntimeError."""
    with patch.dict(os.environ, {"BACKGROUND_REASONER": "potato"}, clear=False):
        from manual_test_console.server import _construct_background_reasoner
        with pytest.raises(RuntimeError, match="Unknown BACKGROUND_REASONER"):
            _construct_background_reasoner()
