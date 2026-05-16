"""_NullDeicticModel stub tests (v0.1j Tasks 4+7)."""

import inspect

from companion_harness.deictic_detector import _NullDeicticModel


def test_null_deictic_model_returns_false_zero():
    model = _NullDeicticModel()
    result = model("what is this?", None)
    assert result == (False, 0.0)


def test_null_deictic_model_marker_exists():
    source = inspect.getsource(_NullDeicticModel)
    assert "# UNAVAILABLE: #169" in source
